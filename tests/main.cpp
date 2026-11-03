#define DOCTEST_CONFIG_IMPLEMENT
#include <doctest/doctest.h>

#include "premarket/parquet_io.hpp"

int main(int argc, char** argv) {
    premarket::parquet::Reader reader;
    return doctest::Context(argc, argv).run();
}
