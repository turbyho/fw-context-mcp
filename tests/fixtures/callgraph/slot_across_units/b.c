typedef void (*handler_t)(int);
static handler_t g_cb;
void set_cb(handler_t h) { g_cb = h; }
void fire(int v) { g_cb(v); }
void run(void (*apply)(handler_t, int)) { apply(g_cb, 1); }
