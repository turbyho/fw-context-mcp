#pragma once

struct Parser {
    bool send(const char* command);
    bool recv(const char* expected);
};
