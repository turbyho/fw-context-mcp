#pragma once
#include "parser.h"

struct Driver {
    Parser* _parser;
    int send(int socket_id);
    bool off() {
        return _parser->send("AT+CFUN=0");
    }
};
