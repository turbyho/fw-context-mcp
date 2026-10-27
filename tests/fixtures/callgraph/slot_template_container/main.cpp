#include <array>
void a1(int);
void a2(int);
void b1();
std::array<void (*)(int), 2> ta = {a1, a2};
std::array<void (*)(), 1> tb = {b1};
void run() {
    ta[0](1);
    tb[0]();
}
