// A single-producer, single-consumer ring of fixed-size CSI slots.
//
// The producer is the WiFi driver task on Core 0, inside the CSI callback. The consumer is the
// packer task on Core 1. There is exactly one of each, forever, which is what makes a lock-free
// ring correct here — and the reason it is worth having: the callback must never block.
//
// Blocking inside the WiFi task is what causes dropped frames and watchdog trips. So the
// producer side has no mutex, no queue send with a timeout, and no allocation. It memcpys into
// a preallocated slot and returns. When the ring is full it drops the frame and increments a
// counter, because a dropped frame is a sequence gap the server can measure, while a stalled
// WiFi task is a dead node.

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// Sized for the largest frame any supported node produces: 256 subcarriers at two bytes each,
// plus the header. HT20 uses a third of this; the memory is cheap and a fixed slot size keeps
// the ring index arithmetic trivial.
#define CSI_SLOT_BYTES 534

// Must be a power of two — the index arithmetic masks rather than divides. 64 slots is about
// 0.8 s of headroom at 80 Hz, which is far more than the sender ever needs and cheap enough
// (~34 KB) not to think about.
#ifndef CSI_RING_SLOTS
#define CSI_RING_SLOTS 64
#endif

typedef struct {
    uint8_t data[CSI_SLOT_BYTES];
    uint16_t len;
} csi_slot_t;

typedef struct {
    csi_slot_t slots[CSI_RING_SLOTS];
    volatile uint32_t head;  // written by the producer only
    volatile uint32_t tail;  // written by the consumer only
    volatile uint32_t dropped;
} csi_ring_t;

void csi_ring_init(csi_ring_t *ring);

// Producer side. Copies `len` bytes into the next slot. Returns false and counts a drop if the
// ring is full or the payload does not fit. Never blocks.
bool csi_ring_push(csi_ring_t *ring, const void *src, size_t len);

// Consumer side. Returns a pointer to the oldest slot, or NULL if empty. The slot stays valid
// until csi_ring_pop_done is called.
const csi_slot_t *csi_ring_peek(csi_ring_t *ring);
void csi_ring_pop_done(csi_ring_t *ring);

uint32_t csi_ring_count(const csi_ring_t *ring);
uint32_t csi_ring_dropped(const csi_ring_t *ring);
