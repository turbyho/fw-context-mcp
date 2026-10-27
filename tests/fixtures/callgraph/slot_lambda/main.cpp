#include <functional>
typedef void (*isr_t)(void);
static isr_t g_isr;
void attach_isr(int pin, isr_t cb) { (void)pin; g_isr = cb; }
void irq_handler() { g_isr(); }
struct Client {
    std::function<void(int)> on_done;
    void handle(int) {}
    void start() { on_done = [this](int x) { handle(x); }; }
    void finish() { on_done(1); }
};
void setup() { attach_isr(2, []() {}); }
