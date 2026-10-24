#include "driver.h"

int Driver::send(int socket_id) {
    return _parser->send("AT+SEND") ? socket_id : -1;
}

bool Driver::off() {
    return _parser->send("AT+CFUN=0") && _parser->recv("OK");
}
