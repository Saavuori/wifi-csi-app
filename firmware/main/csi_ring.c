#include "csi_ring.h"

#include <stddef.h>
#include <string.h>

// Head and tail are free-running counters, masked on use. That removes the classic
// "full looks like empty" ambiguity without wasting a slot, as long as CSI_RING_SLOTS is a
// power of two — which the assert below enforces at compile time.
_Static_assert((CSI_RING_SLOTS & (CSI_RING_SLOTS - 1)) == 0, "CSI_RING_SLOTS must be a power of two");

#define RING_MASK (CSI_RING_SLOTS - 1)

void csi_ring_init(csi_ring_t *ring) {
    ring->head = 0;
    ring->tail = 0;
    ring->dropped = 0;
}

bool csi_ring_push(csi_ring_t *ring, const void *src, size_t len) {
    if (len > CSI_SLOT_BYTES) {
        ring->dropped++;
        return false;
    }

    uint32_t head = ring->head;
    uint32_t tail = ring->tail;  // may be stale; a stale read only ever makes us look fuller
    if ((head - tail) >= CSI_RING_SLOTS) {
        ring->dropped++;
        return false;
    }

    csi_slot_t *slot = &ring->slots[head & RING_MASK];
    memcpy(slot->data, src, len);
    slot->len = (uint16_t)len;

    // Publish the payload before publishing the index. Without this barrier the consumer on
    // the other core can observe the incremented head while the memcpy is still in flight, and
    // send a half-written frame.
    __atomic_store_n(&ring->head, head + 1, __ATOMIC_RELEASE);
    return true;
}

const csi_slot_t *csi_ring_peek(csi_ring_t *ring) {
    uint32_t tail = ring->tail;
    uint32_t head = __atomic_load_n(&ring->head, __ATOMIC_ACQUIRE);
    if (head == tail) {
        return NULL;
    }
    return &ring->slots[tail & RING_MASK];
}

void csi_ring_pop_done(csi_ring_t *ring) {
    __atomic_store_n(&ring->tail, ring->tail + 1, __ATOMIC_RELEASE);
}

// NULL reads as an empty ring. Both accessors exist to be printed by the reporting task, which
// runs in every role — including the transmitter, which starts neither capture nor the sender
// and so leaves both of their ring pointers NULL. Reading through one of those on an ESP32 is a
// LoadProhibited panic, and the node reboots into it again ten seconds later.
uint32_t csi_ring_count(const csi_ring_t *ring) {
    return ring != NULL ? ring->head - ring->tail : 0;
}

uint32_t csi_ring_dropped(const csi_ring_t *ring) {
    return ring != NULL ? ring->dropped : 0;
}
