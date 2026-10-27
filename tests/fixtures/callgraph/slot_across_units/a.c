typedef void (*handler_t)(int);
void set_cb(handler_t h);
void run(void (*apply)(handler_t, int));
static void on_rx(int v) { (void)v; }
static void on_tx(int v) { (void)v; }
static void apply(handler_t g, int v) { g(v); }
void init(void) {
    set_cb(on_rx);
    run(apply);
}
