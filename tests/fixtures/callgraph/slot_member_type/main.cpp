struct A { void f(); };
struct B { void h(); };
using ma = void (A::*)();
using fn_t = void (*)();
extern ma *raw_m;
extern void *raw;
void n() noexcept;
void plain();
void take(A &a) {
    void (B::*q)() = &B::h;
    (void)q;
    ma p = &A::f;
    (void)p;
    fn_t x = n;
    fn_t y = plain;
    (void)x; (void)y;
    (a.*(*raw_m))();
    ((fn_t)raw)();
}
