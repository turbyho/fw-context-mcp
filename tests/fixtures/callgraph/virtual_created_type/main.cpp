struct Base {
    virtual void handler();
};

struct Made : Base {
    void handler() override;
};

struct Never : Base {
    void handler() override;
};

void fire(Base* event) {
    event->handler();
}

Made made;
