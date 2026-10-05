#pragma once
#include <WiFiClientSecure.h>

// How this firmware makes a TLS connection to the internet — in one place,
// because "do we check the certificate" is not a question that should have a
// different answer in each file.
namespace tls {

// Attach the root CA store the Arduino core embeds, so the certificate is
// actually verified.
//
// WHAT THIS FIXES. Every outbound call used to run setInsecure(): the account
// sign-in, the token refresh, the pairing, the Firestore import and the
// firmware update. Nothing checked who answered. Anyone able to intercept the
// connection — a rogue access point, ARP or DNS on a shared network — could
// read the TigerTag refresh token off the wire, and could serve a firmware
// image together with a matching checksum, which the device would verify
// against the attacker's own hash and install.
//
// WHY THE BUNDLE RATHER THAN A PINNED CERTIFICATE. Pinning one root is smaller,
// and it breaks the day a host changes certificate authority — the update
// channel stops working on every device in the field, which is worse than the
// problem it solves. The bundle costs flash and nothing else. It is also why
// this is a function rather than a constant: the next host added gets checked
// without anybody remembering to.
//
// This proves WHO you are talking to. It does not prove who produced the
// firmware — that would be a signature, and see docs/OTA.md for why that is a
// separate decision with its own condition.
void secure(WiFiClientSecure& client);

// Every mbedTLS allocation goes to PSRAM from here on, falling back to internal
// RAM only when PSRAM is out. Called once, first thing in setup().
//
// The core is built with CONFIG_MBEDTLS_INTERNAL_MEM_ALLOC: each TLS session's
// buffers - about 40 KB - came out of the ~320 KB of internal RAM that Wi-Fi,
// the panel and every task share, while 8 MB of PSRAM sat free. Three sessions
// was the ceiling, and a fourth, one-off request - the presence beat beside a
// LAN Bambu and two cloud sessions - failed its handshake every time.
void usePsram();

}  // namespace tls
