void serve() {}

int plan(int item) {
    // An empty command is reported in serve() as "already set".
    /* write_all() is not called here either */
    return item + 1;
}
