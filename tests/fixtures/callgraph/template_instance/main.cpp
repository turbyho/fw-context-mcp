struct Sensor {
    int sample();
};

template <class T> int read_twice(T& device) {
    return device.sample() + device.sample();
}

int poll(Sensor& sensor) {
    return read_twice(sensor);
}
