struct Tag {
    Tag() {}
    explicit Tag(int value) { (void)value; }
};

extern const Tag tag;

template <class T> void take(T value, const Tag& marker);
template <class T> void take(T value, int count);

template <class T> void use(T value) {
    take(value, tag);
}
