struct source {
    int (*read)(int offset);
};

static int read_flash(int offset) { return offset; }
static int read_card(int offset) { return -offset; }

void setup(struct source* src) {
    src->read = read_flash;
}

int load(struct source* src) {
    return src->read(4);
}
