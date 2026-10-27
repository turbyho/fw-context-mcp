typedef void (*cb_t)(int);
void handler(int);
struct Dev { cb_t cb_ = handler; cb_t &cb() { return cb_; } };
void use(Dev &d) { d.cb()(1); }
