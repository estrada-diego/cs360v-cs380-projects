/* virtio.c: STUDENT IMPLEMENTATION FILE for Project 1 (Part III).
 *
 * In Part II you invented a device protocol (MMIO registers). Here you implement
 * the one the whole world actually uses: a virtio split virtqueue, driven by a
 * REAL QEMU virtual machine, whose stock virtio_console driver talks to your
 * code with no guest-side changes at all.
 *
 * The provided backend (backend.c) speaks the vhost-user protocol to QEMU, maps
 * the guest's memory, sets up the rings, and calls you every time the guest
 * kicks the queue. Everything below the seam is yours: TWO functions.
 *
 * WHAT IS PROVIDED: virtq.h (the ring structures, the memory map, the log sink),
 * backend.c (all vhost-user plumbing), run-qemu.sh (boots the VM).
 *
 * ============================================================================
 * TASK (a): virtq_gpa_to_hva(), the hypervisor's address translation.
 * ============================================================================
 * You wrote this once already: vmm_gpa_to_host() in Part I. This is the same
 * contract against a real hypervisor's memory map, except now there can be
 * SEVERAL regions with GAPS between them (see `struct virtq_mem` in virtq.h).
 *
 * Return a host pointer for [gpa, gpa+len), or NULL unless the range lies
 * ENTIRELY inside ONE region. The guest picks these numbers, so reject:
 *   - a range that runs off the end of its region (no straddling two regions),
 *   - an address in a gap between regions, or outside all of them,
 *   - a gpa + len that overflows.
 * Get this wrong and the guest can make your hypervisor read (or crash on)
 * memory that is not its own.
 *
 * ============================================================================
 * TASK (b): vlog_virtq_handle(), the virtqueue.
 * ============================================================================
 * For every chain the guest has made available:
 *
 *   1. Read the head index from the AVAIL ring:
 *          head = vq->avail->ring[vq->last_avail % vq->num]
 *      (First read vq->avail->idx to see how many are ready, and virtq_rmb()
 *      before you trust the ring contents.)
 *   2. Walk the DESCRIPTOR CHAIN from `head`. For each descriptor:
 *        - VRING_DESC_F_WRITE set  -> it is space for the DEVICE to write into.
 *          This queue is guest->host, so it carries no data: skip it.
 *        - VRING_DESC_F_INDIRECT set -> it is not data either! `d->addr` points
 *          at a TABLE of further descriptors in guest memory (`d->len` bytes of
 *          them, so d->len / sizeof(struct vring_desc) entries), which form
 *          their own chain starting at index 0. Translate the table and walk it.
 *          (Indirect tables do not nest.)
 *        - otherwise it is data: translate d->addr with
 *              virtq_gpa_to_hva(mem, d->addr, d->len)
 *          (CHECK FOR NULL) and append its bytes to the record.
 *        - follow d->next while VRING_DESC_F_NEXT is set.
 *      Concatenate the chain's bytes into one record, capped at VIRTQ_MAX_RECORD
 *      (never overflow your buffer).
 *   3. Emit it:  vlog_sink_emit(sink, bytes, len);
 *   4. COMPLETE the chain on the USED ring:
 *          vq->used->ring[vq->used->idx % vq->num] = { .id = head, .len = 0 };
 *          virtq_wmb();
 *          vq->used->idx++;
 *      (This is the virtio equivalent of the Part II STATUS/SEQ readback: it is
 *      how the driver learns its buffer is free again. Get it wrong and the
 *      guest hangs after a few writes.)
 *   5. Advance vq->last_avail and count the chain.
 *
 * Return the number of chains you completed; the backend raises the guest's
 * interrupt when that is > 0.
 *
 * SAFETY: the rings live in memory the GUEST owns and can change at any time. A
 * descriptor index, a chain, or a length can be nonsense. Bounds-check every
 * index against the table size, check every translation, and never loop forever
 * on a cyclic chain. A hypervisor must not be crashable by its guest.
 *
 * See SPEC.md Part III. Develop against `cd tests && ./run_virtq_tests.sh`, then
 * watch it drive a real VM with `cd vhost && ./run-qemu.sh`.
 */
#include "virtq.h"

#include <stdbool.h>
#include <string.h>

/* ---- (a) guest-physical -> host-virtual -------------------------------- */

void *virtq_gpa_to_hva(const struct virtq_mem *mem, uint64_t gpa, uint64_t len)
{
    if (len == 0 || gpa + len < gpa)
        return NULL;

    for (unsigned i = 0; i < mem->nregions; i++) {
        const struct virtq_mem_region *r = &mem->regions[i];
        if (gpa < r->gpa)
            continue;
        uint64_t off = gpa - r->gpa;
        if (off < r->size && len <= r->size - off)
            return r->hva + off;
    }
    return NULL;
}

/* ---- (b) the virtqueue ------------------------------------------------- */

/* Snapshot a descriptor with one load per field: the guest can rewrite it
 * between a check and a use, so only ever act on this copy. */
static struct vring_desc read_desc(const struct vring_desc *src)
{
    return (struct vring_desc){
        .addr  = __atomic_load_n(&src->addr,  __ATOMIC_RELAXED),
        .len   = __atomic_load_n(&src->len,   __ATOMIC_RELAXED),
        .flags = __atomic_load_n(&src->flags, __ATOMIC_RELAXED),
        .next  = __atomic_load_n(&src->next,  __ATOMIC_RELAXED),
    };
}

/* Append one readable descriptor's bytes to rec, truncating at the cap.
 * Returns false if the guest gave us a range that does not translate. */
static bool append_desc(const struct virtq_mem *mem, const struct vring_desc *d,
                        char *rec, uint32_t *len)
{
    if (d->len == 0)
        return true;
    const void *src = virtq_gpa_to_hva(mem, d->addr, d->len);
    if (!src)
        return false;
    uint32_t room = VIRTQ_MAX_RECORD - *len;
    uint32_t n = d->len < room ? d->len : room;
    memcpy(rec + *len, src, n);
    *len += n;
    return true;
}

/* Walk the chain starting at tbl[i], appending readable data to rec. A valid
 * chain visits each of the n entries at most once, so n hops bounds a cycle.
 * Returns false if the chain is malformed; whatever was gathered stays in rec. */
static bool walk_chain(const struct vring_desc *tbl, unsigned n, unsigned i,
                       bool nested, const struct virtq_mem *mem,
                       char *rec, uint32_t *len)
{
    for (unsigned hops = 0; hops < n; hops++) {
        if (i >= n)
            return false;
        struct vring_desc d = read_desc(&tbl[i]);

        if (d.flags & VRING_DESC_F_INDIRECT) {
            if (nested)
                return false;
            unsigned m = d.len / sizeof(struct vring_desc);
            const struct vring_desc *itbl =
                m ? virtq_gpa_to_hva(mem, d.addr, (uint64_t)m * sizeof *itbl) : NULL;
            if (!itbl || !walk_chain(itbl, m, 0, true, mem, rec, len))
                return false;
        } else if (!(d.flags & VRING_DESC_F_WRITE)) {
            if (!append_desc(mem, &d, rec, len))
                return false;
        }

        if (!(d.flags & VRING_DESC_F_NEXT))
            return true;
        i = d.next;
    }
    return false;
}

int vlog_virtq_handle(struct virtq *vq, const struct virtq_mem *mem,
                      struct vlog_sink *sink)
{
    uint16_t avail_idx = __atomic_load_n(&vq->avail->idx, __ATOMIC_RELAXED);
    virtq_rmb();

    /* more outstanding than the ring can hold means a broken driver */
    if ((uint16_t)(avail_idx - vq->last_avail) > vq->num)
        return 0;

    int done = 0;
    while (vq->last_avail != avail_idx) {
        uint16_t head = __atomic_load_n(&vq->avail->ring[vq->last_avail % vq->num],
                                        __ATOMIC_RELAXED);
        char rec[VIRTQ_MAX_RECORD];
        uint32_t len = 0;

        if (head < vq->num)
            walk_chain(vq->desc, vq->num, head, false, mem, rec, &len);
        if (len)
            vlog_sink_emit(sink, rec, len);

        vq->used->ring[vq->used->idx % vq->num] =
            (struct vring_used_elem){ .id = head, .len = 0 };
        virtq_wmb();
        vq->used->idx++;

        vq->last_avail++;
        done++;
    }
    return done;
}
