// Host-side test for the parts of the firmware that are plain C: the SPSC ring and the wire
// header layout. Neither needs ESP-IDF, and both are exactly the places where a mistake is
// invisible on the device — a torn frame looks like noise, and a padding byte looks like packet
// loss.
//
//   cc -O2 -pthread -I../main -o /tmp/test_ring test_ring.c ../main/csi_ring.c && /tmp/test_ring

#include <assert.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "csi_ring.h"
#include "csi_wire.h"

#define ITERATIONS 200000

static csi_ring_t g_ring;
static volatile int g_producer_done;
static uint32_t g_consumed;
static uint32_t g_corrupt;

// Each frame is filled with a byte pattern derived from its sequence number, so a torn frame —
// the failure a missing memory barrier produces — is detectable rather than merely suspected.
static void fill(uint8_t *buf, size_t len, uint32_t seq) {
    for (size_t i = 0; i < len; i++) {
        buf[i] = (uint8_t)(seq + i);
    }
}

static int check(const uint8_t *buf, size_t len) {
    uint32_t seq = buf[0];
    for (size_t i = 0; i < len; i++) {
        if (buf[i] != (uint8_t)(seq + i)) {
            return 0;
        }
    }
    return 1;
}

static void *producer(void *arg) {
    (void)arg;
    uint8_t frame[150];
    for (uint32_t i = 0; i < ITERATIONS; i++) {
        fill(frame, sizeof(frame), i);
        csi_ring_push(&g_ring, frame, sizeof(frame));
    }
    __atomic_store_n(&g_producer_done, 1, __ATOMIC_RELEASE);
    return NULL;
}

static void *consumer(void *arg) {
    (void)arg;
    while (1) {
        const csi_slot_t *slot = csi_ring_peek(&g_ring);
        if (slot == NULL) {
            if (__atomic_load_n(&g_producer_done, __ATOMIC_ACQUIRE)) {
                if (csi_ring_peek(&g_ring) == NULL) {
                    return NULL;
                }
                continue;
            }
            continue;
        }
        if (!check(slot->data, slot->len)) {
            g_corrupt++;
        }
        g_consumed++;
        csi_ring_pop_done(&g_ring);
    }
}

static void test_wire_layout(void) {
    // The layout the server parses. Getting this wrong is the single most expensive mistake
    // available here: it produces plausible-looking garbage rather than an error.
    assert(sizeof(csi_wire_header_t) == 22);

    csi_wire_header_t header;
    uint8_t *raw = (uint8_t *)&header;
    memset(raw, 0, sizeof(header));

    header.magic = CSI_WIRE_MAGIC;
    header.version = CSI_WIRE_VERSION;
    header.node_id = 3;
    header.seq = 0xDEADBEEF;
    header.timestamp = 0x0102030405060708ULL;
    header.rssi = -47;
    header.noise_floor = -92;
    header.channel = 6;
    header.sec_channel = 0;
    header.n_sub = 64;

    assert(raw[0] == 0x53 && raw[1] == 0x43);  // 0x4353 little-endian
    assert(raw[2] == 1);
    assert(raw[3] == 3);
    assert(raw[4] == 0xEF && raw[7] == 0xDE);
    assert(raw[8] == 0x08 && raw[15] == 0x01);
    assert((int8_t)raw[16] == -47);
    assert((int8_t)raw[17] == -92);
    assert(raw[18] == 6);
    assert(raw[19] == 0);
    assert(raw[20] == 64 && raw[21] == 0);

    printf("wire layout: 22 bytes, offsets match the spec\n");
}

static void test_ring_basics(void) {
    csi_ring_init(&g_ring);
    assert(csi_ring_peek(&g_ring) == NULL);
    assert(csi_ring_count(&g_ring) == 0);

    uint8_t frame[64];
    fill(frame, sizeof(frame), 7);
    assert(csi_ring_push(&g_ring, frame, sizeof(frame)));
    assert(csi_ring_count(&g_ring) == 1);

    const csi_slot_t *slot = csi_ring_peek(&g_ring);
    assert(slot != NULL && slot->len == sizeof(frame));
    assert(memcmp(slot->data, frame, sizeof(frame)) == 0);
    csi_ring_pop_done(&g_ring);
    assert(csi_ring_peek(&g_ring) == NULL);

    // Oversize payloads are dropped, not truncated. Truncating would send a frame whose n_sub
    // disagrees with its length, which the server rejects — but only after it has already been
    // recorded as a bad packet for hours.
    uint8_t huge[CSI_SLOT_BYTES + 1];
    assert(!csi_ring_push(&g_ring, huge, sizeof(huge)));
    assert(csi_ring_dropped(&g_ring) == 1);

    printf("ring basics: push/peek/pop and oversize rejection\n");
}

static void test_drop_on_full(void) {
    csi_ring_init(&g_ring);
    uint8_t frame[64];
    fill(frame, sizeof(frame), 1);

    for (int i = 0; i < CSI_RING_SLOTS; i++) {
        assert(csi_ring_push(&g_ring, frame, sizeof(frame)));
    }
    // The whole point: a full ring drops and returns immediately. It never blocks, because the
    // caller is the WiFi driver task and blocking there is what trips the watchdog.
    assert(!csi_ring_push(&g_ring, frame, sizeof(frame)));
    assert(csi_ring_dropped(&g_ring) == 1);
    assert(csi_ring_count(&g_ring) == CSI_RING_SLOTS);

    printf("ring full: drops rather than blocking\n");
}

static void test_concurrent(void) {
    csi_ring_init(&g_ring);
    g_producer_done = 0;
    g_consumed = 0;
    g_corrupt = 0;

    pthread_t p, c;
    pthread_create(&c, NULL, consumer, NULL);
    pthread_create(&p, NULL, producer, NULL);
    pthread_join(p, NULL);
    pthread_join(c, NULL);

    uint32_t dropped = csi_ring_dropped(&g_ring);
    printf("concurrent: %u consumed, %u dropped, %u corrupt\n", g_consumed, dropped, g_corrupt);
    assert(g_corrupt == 0);
    assert(g_consumed + dropped == ITERATIONS);
}

int main(void) {
    test_wire_layout();
    test_ring_basics();
    test_drop_on_full();
    test_concurrent();
    printf("all firmware host tests passed\n");
    return 0;
}
