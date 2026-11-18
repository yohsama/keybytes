#!/usr/bin/env python3
"""Apply bounded random-delay and random-hold support to yeroca Concurrent Macros.

The upstream module is cloned at build time. Each exact-match replacement is checked,
so a changed upstream implementation fails the build rather than silently patching
the wrong code.
"""
from pathlib import Path

path = Path("keychron_qmk/modules/qmk_concurrent_macros/concurrent_macros/concurrent_macros.c")
src = path.read_text(encoding="utf-8")


def replace_exact(old: str, new: str, label: str) -> None:
    global src
    count = src.count(old)
    if count != 1:
        raise RuntimeError(f"Patch anchor {label!r}: expected 1 match, found {count}")
    src = src.replace(old, new, 1)


replace_exact(
    '#include "send_string.h"\n',
    '#include "send_string.h"\n#include "timer.h"\n',
    "timer include",
)
replace_exact(
    "CM_ERROR } concurrent_macros_state_t;",
    "CM_RELEASE_RANDOM_HOLD, CM_ERROR } concurrent_macros_state_t;",
    "hold-release state",
)
replace_exact(
    "    uint16_t                  offset;\n",
    "    uint16_t                  offset;\n"
    "    uint8_t                   random_hold_key;\n",
    "hold-key state",
)

random_helpers = r'''
// Keycodes F18/F19 are handled locally by this module; no extra VIA editor
// plugin is necessary. Syntax:
//   {KC_F19}MIN,MAX;                   Random delay (ms), inclusive.
//   {KC_F18}{KC_A}MIN,MAX;             Hold KC_A for random duration (ms).
// To avoid over-reading into the next macro, each command must terminate with
// the literal semicolon and contain two decimal numbers in [0, 9999].
#ifndef KC_RANDOM_HOLD
#    define KC_RANDOM_HOLD KC_F18
#endif
#ifndef KC_RANDOM_DELAY
#    define KC_RANDOM_DELAY KC_F19
#endif

// Non-cryptographic PRNG, seeded on first use using trigger-time jitter.
// A shared state is intentional: two concurrent macros do not restart the
// same random sequence on each iteration.
static uint32_t cm_random_state = 0;

static uint32_t cm_random_next(void) {
    if (cm_random_state == 0) {
        cm_random_state = timer_read32() ^ 0x9E3779B9u;
        if (cm_random_state == 0) cm_random_state = 0xA341316Cu;
    }
    uint32_t x = cm_random_state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    cm_random_state = x;
    return x;
}

static uint32_t cm_random_between(uint16_t min_ms, uint16_t max_ms) {
    const uint32_t width = (uint32_t)max_ms - min_ms + 1u;
    // Rejection sampling makes each integer in the requested interval
    // equally likely without introducing a modulo bias.
    const uint32_t cutoff = UINT32_MAX - (UINT32_MAX % width);
    uint32_t random_value;
    do {
        random_value = cm_random_next();
    } while (random_value >= cutoff);
    return (uint32_t)min_ms + (random_value % width);
}

static bool cm_read_nonzero_byte(concurrent_macros_info_t *macro, uint8_t *out) {
    if (macro->offset >= dynamic_keymap_macro_get_buffer_size()) {
        return false;
    }
    dynamic_keymap_macro_get_buffer(macro->offset++, 1, out);
    return *out != 0;
}

static bool cm_read_decimal(concurrent_macros_info_t *macro, char delimiter, uint16_t *out) {
    uint16_t value = 0;
    uint8_t digits = 0;
    uint8_t ch;
    while (cm_read_nonzero_byte(macro, &ch)) {
        if (ch == (uint8_t)delimiter && digits > 0) {
            *out = value;
            return true;
        }
        if (ch < '0' || ch > '9' || digits == 4) {
            return false;
        }
        value = (uint16_t)(value * 10 + (ch - '0'));
        digits++;
    }
    return false;
}

static bool cm_read_random_bounds(concurrent_macros_info_t *macro, uint16_t *min_ms, uint16_t *max_ms) {
    return cm_read_decimal(macro, ',', min_ms) &&
           cm_read_decimal(macro, ';', max_ms) &&
           *min_ms <= *max_ms;
}

// The key immediately after KC_F18 must be an actual VIA key action,
// such as {KC_A}, and not plain text "a".
static bool cm_read_hold_key(concurrent_macros_info_t *macro, uint8_t *key) {
    uint8_t prefix, action, usage;
    if (!cm_read_nonzero_byte(macro, &prefix) ||
        !cm_read_nonzero_byte(macro, &action) ||
        !cm_read_nonzero_byte(macro, &usage)) {
        return false;
    }
    if (prefix != SS_QMK_PREFIX || action != SS_TAP_CODE) {
        return false;
    }
    *key = usage;
    return true;
}
'''
replace_exact(
    "deferred_executor_t deferred_executor_table[MAX_CONCURRENT_MACROS];\n",
    "deferred_executor_t deferred_executor_table[MAX_CONCURRENT_MACROS];\n" + random_helpers,
    "random helpers",
)

random_cases = r'''                                case KC_RANDOM_DELAY: {
                                    uint16_t min_ms, max_ms;
                                    if (!cm_read_random_bounds(macro, &min_ms, &max_ms)) {
                                        macro->state = CM_ERROR;
                                        return 0;
                                    }
                                    return MAX(1u, cm_random_between(min_ms, max_ms));
                                }
                                case KC_RANDOM_HOLD: {
                                    uint8_t held_key;
                                    uint16_t min_ms, max_ms;
                                    if (!cm_read_hold_key(macro, &held_key) ||
                                        !cm_read_random_bounds(macro, &min_ms, &max_ms)) {
                                        macro->state = CM_ERROR;
                                        return 0;
                                    }
                                    const uint32_t duration = cm_random_between(min_ms, max_ms);
                                    const uint8_t down[] = {SS_QMK_PREFIX, SS_DOWN_CODE, held_key, 0};
                                    send_string_with_delay((const char *)down, 0);
                                    macro->random_hold_key = held_key;
                                    macro->state = CM_RELEASE_RANDOM_HOLD;
                                    return MAX(1u, duration);
                                }
'''
replace_exact(
    "                                case KC_HARD_STOP_OTHER_MACROS:\n",
    random_cases + "                                case KC_HARD_STOP_OTHER_MACROS:\n",
    "random action cases",
)

release_case = r'''        case CM_RELEASE_RANDOM_HOLD: {
            // The hold is only a timed key-down, not a blocking wait.
            // Process the next command on the following scheduler tick.
            const uint8_t up[] = {SS_QMK_PREFIX, SS_UP_CODE, macro->random_hold_key, 0};
            send_string_with_delay((const char *)up, 0);
            macro->state = CM_DO_NEXT;
            return 1;
        }
'''
replace_exact(
    "        case CM_ERROR:\n            // this macro encountered an error.",
    release_case + "        case CM_ERROR:\n            // this macro encountered an error.",
    "random hold release callback",
)
replace_exact(
    "        case CM_DO_NEXT:\n#ifdef CONCURRENT_MACROS_MOUSE_ENABLE\n        case CM_REPEAT_REPORT_MOUSE:\n#endif\n            // macro is running already",
    "        case CM_DO_NEXT:\n        case CM_RELEASE_RANDOM_HOLD:\n"
    "#ifdef CONCURRENT_MACROS_MOUSE_ENABLE\n        case CM_REPEAT_REPORT_MOUSE:\n#endif\n"
    "            // macro is running already",
    "repeat toggle during random hold",
)
replace_exact(
    "                case CM_DO_NEXT:\n#ifdef CONCURRENT_MACROS_MOUSE_ENABLE\n"
    "                case CM_REPEAT_REPORT_MOUSE:\n#endif\n                    if (macro->flags & CM_FLAG_STOP)",
    "                case CM_DO_NEXT:\n                case CM_RELEASE_RANDOM_HOLD:\n"
    "#ifdef CONCURRENT_MACROS_MOUSE_ENABLE\n"
    "                case CM_REPEAT_REPORT_MOUSE:\n#endif\n                    if (macro->flags & CM_FLAG_STOP)",
    "LED state during random hold",
)

path.write_text(src, encoding="utf-8")
assert "case KC_RANDOM_DELAY" in src
assert "case KC_RANDOM_HOLD" in src
print(f"Patched {path}: uniform random delay and non-blocking random hold enabled")
