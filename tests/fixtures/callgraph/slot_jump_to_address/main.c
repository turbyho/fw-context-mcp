typedef void (*entry_t)(void);
void SysTick_Handler(void) {}
static void (*const vectors[])(void) = { SysTick_Handler };
void jump(void) { ((entry_t)(*(unsigned int *)0x08004004))(); }
const void *keep(void) { return vectors; }
