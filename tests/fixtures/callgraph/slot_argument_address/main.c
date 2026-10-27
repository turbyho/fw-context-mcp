typedef void (*cb_t)(int);
struct ops { void (*reg)(cb_t); };
extern struct ops *g_ops;
void on_done(int x) { (void)x; }
void run(void) { g_ops->reg(on_done); }
extern void *opaque;
void fire(void) { ((cb_t)opaque)(1); }
