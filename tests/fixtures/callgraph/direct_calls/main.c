static int helper(int v) { return v + 1; }
int twice(int v) { return helper(v) * 2; }
int main(void) { return twice(3); }
