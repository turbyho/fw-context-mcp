typedef void (*cb_t)(void);
void ext_register(cb_t *slot);
static cb_t hook;
static void first(void) {}
static void second(void) {}
static void never(void) {}
void setup(void) { hook = first; ext_register(&hook); }
void keep(void) { cb_t other = second; (void)other; }
void tick(void) { hook(); }
