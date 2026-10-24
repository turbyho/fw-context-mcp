struct Parser {
    bool send(int value) { return value > 0; }
};

struct Driver {
    Parser* _parser;
    bool off();
};

bool Driver::off() {
    // _parser->send(1) is not called here
    return _parser != nullptr;
}
