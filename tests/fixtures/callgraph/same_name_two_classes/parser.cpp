#include "parser.h"

bool Parser::send(const char* command) { return command != nullptr; }
bool Parser::recv(const char* expected) { return expected != nullptr; }
