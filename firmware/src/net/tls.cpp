#include "tls.h"
#include <esp_heap_caps.h>
#include <mbedtls/platform.h>

// The root CA store the Arduino core embeds and links into every build. The
// symbol is emitted by the core's own build, so nothing is added to this
// repository and nothing has to be kept up to date by hand.
extern const uint8_t rootca_crt_bundle_start[] asm("_binary_x509_crt_bundle_start");

namespace tls {

void secure(WiFiClientSecure& client) {
    client.setCACertBundle(rootca_crt_bundle_start);
    // Long enough for a handshake on a slow uplink, short enough that a screen
    // waiting on it does not look wedged.
    client.setHandshakeTimeout(15);
}

namespace {
void* psramCalloc(size_t n, size_t size) {
    void* p = heap_caps_calloc(n, size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    return p ? p : heap_caps_calloc(n, size, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
}
// heap_caps_free() takes a block from either heap - including one allocated
// internally before usePsram() ran.
void anyFree(void* p) { heap_caps_free(p); }
}  // namespace

void usePsram() {
    mbedtls_platform_set_calloc_free(psramCalloc, anyFree);
}

}  // namespace tls
