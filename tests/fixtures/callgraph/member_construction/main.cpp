struct Foo {
    Foo() {}
    explicit Foo(int value) { (void)value; }
};

struct Named {
    Foo m;
    Foo n;
    Named()
        : m(1) {}
};

struct WithInit {
    Foo m{5};
    WithInit() {}
};

struct Implicit {
    Foo m;
};

struct Nested {
    Implicit inner;
};

Implicit imp;
Nested nest;

void make() {
    Implicit local;
    (void)local;
}

struct Delegating {
    Foo m;
    Delegating()
        : Delegating(1) {}
    explicit Delegating(int value)
        : m(value) {}
};
