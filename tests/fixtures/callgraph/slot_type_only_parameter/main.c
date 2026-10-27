typedef void (*cb_t)(int);
typedef void (*apply_t)(cb_t);
void leaf(int x) { (void)x; }
void apply(cb_t g) { g(7); }
extern void *table[];
void boot(void) {
    apply_t a = apply;
    ((apply_t)table[0])(leaf);
    (void)a;
}
