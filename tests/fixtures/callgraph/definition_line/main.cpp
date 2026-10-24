struct Led {
    Led();
    ~Led();
    int level;
};

Led::Led() : level(0) {}

Led::~Led() {
    level = 0;
}
