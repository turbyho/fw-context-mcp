struct Foo {
    Foo() {}
    explicit Foo(int value) { (void)value; }
};

extern Foo declared;
Foo global_default;
Foo global_arg(7);
static Foo file_static;

struct Holder {
    Foo member;
    Holder() : member(3) {}
};

struct Implicit {
    Foo member;
};

void local() {
    Foo a;
    Foo b(2);
    Foo* c = new Foo(4);
    (void)c;
}

Foo from_temp = Foo(1);
