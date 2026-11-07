/* ANALYST PSEUDOCODE, not original C or a patch. Unresolved side effects retained. */
/* All numeric addresses below refer to VM code words. */

/* 0xBD6..0xBFA: selected activity events */
if (conf1[0x5A] != 0) {
    cancel_all(task_0x508, 0x60D8);
    send_later(task_0x508, 0x60D8, NULL, conf1[0x5A] * 1000);
}
/* 0x9200..0x921B: a separate state-dependent scheduling path.
 * This path has NO equivalent zero-value check. A zero setting alone
 * therefore cannot safely be assumed to disable the idle transition. */
if (state_transition_conditions) {
    cancel_all(task_0x508, 0x60D8);
    send_later(task_0x508, 0x60D8, NULL, conf1[0x5A] * 1000);
}

/* 0xDDA..0xE2D: event 0x60D8 */
if (audio_and_state_guards_pass) {
    call_0x435(1);       /* writes global word +0x54 = 1 */
    call_0x36A9();
    call_0x3366();      /* complementary control to call_0x3360 on resume */
    call_0x326A(1);
    call_0x337E(0);
    if (conf1[0x47] != 0) {
        cancel_all(task_0x508, 0x60DF);
        send_later(task_0x508, 0x60DF, NULL, conf1[0x47] * 1000);
    }
}

/* 0xE2F..0xE66: event 0x60DF */
global_word_0x5A |= 0x4000;
if (audio_and_state_guards_pass) {
    cancel_all(task_0x508, 0x6002);
    send_immediate(task_0x508, 0x6002, NULL);  /* reaches 0xCD1 */
}
/* 0xE68..: resume reverses global +0x54, uses complementary controls,
 * and cancels 0x60DF. Exact external circuit effects remain unverified. */
