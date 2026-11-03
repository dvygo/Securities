#pragma once

#include <cstdint>
#include <filesystem>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace premarket::parquet {

namespace fs = std::filesystem;

inline constexpr std::size_t CHUNK_ROWS = 200000;
inline constexpr const char* COMPRESSION = "zstd";
inline constexpr const char* SUFFIX = ".parquet";

using Cell = std::optional<std::string>;
using Row = std::vector<std::string>;
using NullableRow = std::vector<Cell>;

enum class Type { String, Int32 };

struct Column {
    std::string name;
    Type type = Type::String;
};

class RowWriter {
public:
    RowWriter(fs::path path, std::vector<std::string> columns);
    ~RowWriter();
    RowWriter(const RowWriter&) = delete;
    RowWriter& operator=(const RowWriter&) = delete;

    void write(const std::vector<Row>& rows);
    bool close();
    void abort();
    std::size_t total() const { return total_; }
    const fs::path& path() const { return path_; }

private:
    struct Impl;
    fs::path path_;
    fs::path temp_;
    std::vector<std::string> columns_;
    std::unique_ptr<Impl> impl_;
    std::size_t total_ = 0;

    void open();
    void release();
};

bool write_rows(const fs::path& path, const std::vector<std::string>& columns, const std::vector<Row>& rows);

void write_table(const fs::path& path, const std::vector<Column>& columns, const std::vector<NullableRow>& rows);

struct Table {
    std::vector<std::string> columns;
    std::vector<NullableRow> rows;
    int index(const std::string& name) const;
};

Table read_table(const fs::path& path, const std::vector<std::string>& columns = {});
void iter_rows(const fs::path& path, std::size_t batch_rows,
               const std::function<void(const std::vector<std::string>& columns, const std::vector<Row>& rows)>& fn,
               const std::vector<std::string>& wanted = {});
std::vector<std::string> column_names(const fs::path& path);
std::int64_t row_count(const fs::path& path);
std::string sql_quote(const std::string& s);
std::string sql_ident(const std::string& s);

}  // namespace premarket::parquet
