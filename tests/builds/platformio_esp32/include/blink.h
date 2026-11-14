/* The blink state of the test project of the extended set. */
#ifndef BLINK_H
#define BLINK_H

#ifdef __cplusplus
extern "C" {
#endif

typedef enum { BLINK_OFF = 0, BLINK_ON = 1 } BlinkState;

void blink_init(void);
BlinkState blink_next(BlinkState state);

#ifdef __cplusplus
}
#endif

#endif /* BLINK_H */
