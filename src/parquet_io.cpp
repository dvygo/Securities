#include "premarket/parquet_io.hpp"

#include <duckdb.h>

#include <system_error>

#include "premarket/util.hpp"

namespace premarket::parquet {

namespace {

struct Db {
    duckdb_database db = nullptr;
    duckdb_connection con = nullptr;

    Db(const std::string& file, bool single_thread, const fs::path& temp_dir) {
        duckdb_config cfg = nullptr;
        duckdb_create_config(&cfg);
        duckdb_set_config(cfg, "autoinstall_known_extensions", "false");
        duckdb_set_config(cfg, "autoload_known_extensions", "false");
        if (single_thread) duckdb_set_config(cfg, "threads", "1");
        if (!temp_dir.empty()) duckdb_set_config(cfg, "temp_directory", temp_dir.c_str());
        char* error = nullptr;
        auto state = duckdb_open_ext(file.empty() ? nullptr : file.c_str(), &db, cfg, &error);
        duckdb_destroy_config(&cfg);
        if (state != DuckDBSuccess) {
            std::string msg = error ? error : "unknown error";
            duckdb_free(error);
            throw RuntimeError("duckdb: cannot open database: " + msg);
        }
        if (duckdb_connect(db, &con) != DuckDBSuccess) {
            duckdb_close(&db);
            throw RuntimeError("duckdb: cannot connect");
        }
    }
    ~Db() {
        if (con) duckdb_disconnect(&con);
        if (db) duckdb_close(&db);
    }
    Db(const Db&) = delete;
    Db& operator=(const Db&) = delete;

    void exec(const std::string& sql) {
        duckdb_result result;
        if (duckdb_query(con, sql.c_str(), &result) != DuckDBSuccess) {
            std::string msg = duckdb_result_error(&result) ? duckdb_result_error(&result) : "unknown error";
            duckdb_destroy_result(&result);
            throw RuntimeError("duckdb: " + msg);
        }
        duckdb_destroy_result(&result);
    }
};

Db& reader() {
    static Db db("", false, {});
    return db;
}

std::string type_sql(Type t) { return t == Type::Int32 ? "INTEGER" : "VARCHAR"; }

void remove_quiet(const fs::path& p) {
    std::error_code ec;
    fs::remove(p, ec);
}

std::string copy_sql(const std::string& table, const fs::path& target) {
    return "COPY " + table + " TO " + sql_quote(target.string()) + " (FORMAT PARQUET, COMPRESSION ZSTD)";
}

template <typename Fn>
void query_chunks(const std::string& sql, Fn&& on_chunk) {
    auto& db = reader();
    duckdb_result result;
    if (duckdb_query(db.con, sql.c_str(), &result) != DuckDBSuccess) {
        std::string msg = duckdb_result_error(&result) ? duckdb_result_error(&result) : "unknown error";
        duckdb_destroy_result(&result);
        throw RuntimeError("duckdb: " + msg);
    }
    idx_t ncols = duckdb_column_count(&result);
    std::vector<std::string> names;
    for (idx_t c = 0; c < ncols; ++c) names.emplace_back(duckdb_column_name(&result, c));
    while (true) {
        duckdb_data_chunk chunk = duckdb_fetch_chunk(result);
        if (!chunk) break;
        on_chunk(names, chunk);
        duckdb_destroy_data_chunk(&chunk);
    }
    duckdb_destroy_result(&result);
}

std::string select_list(const fs::path& path, const std::vector<std::string>& columns) {
    std::vector<std::string> cols = columns.empty() ? column_names(path) : columns;
    std::vector<std::string> parts;
    for (const auto& c : cols) parts.push_back("CAST(" + sql_ident(c) + " AS VARCHAR) AS " + sql_ident(c));
    return util::join(parts, ", ");
}

}  // namespace

std::string sql_quote(const std::string& s) { return "'" + util::replace_all(s, "'", "''") + "'"; }

std::string sql_ident(const std::string& s) { return "\"" + util::replace_all(s, "\"", "\"\"") + "\""; }

struct RowWriter::Impl {
    std::unique_ptr<Db> db;
    duckdb_appender appender = nullptr;
    fs::path db_file;
};

RowWriter::RowWriter(fs::path path, std::vector<std::string> columns)
    : path_(std::move(path)), temp_(util::staging_path(path_)), columns_(std::move(columns)) {}

RowWriter::~RowWriter() {
    if (impl_) abort();
}

void RowWriter::open() {
    fs::create_directories(temp_.parent_path());
    impl_ = std::make_unique<Impl>();
    impl_->db_file = fs::path(temp_.string() + ".duckdb");
    remove_quiet(impl_->db_file);
    remove_quiet(impl_->db_file.string() + ".wal");
    impl_->db = std::make_unique<Db>(impl_->db_file.string(), true, temp_.parent_path());
    std::vector<std::string> defs;
    for (const auto& c : columns_) defs.push_back(sql_ident(c) + " VARCHAR NOT NULL");
    impl_->db->exec("SET memory_limit='1GB'");
    impl_->db->exec("SET preserve_insertion_order=true");
    impl_->db->exec("CREATE TABLE t (" + util::join(defs, ", ") + ")");
    if (duckdb_appender_create(impl_->db->con, nullptr, "t", &impl_->appender) != DuckDBSuccess)
        throw RuntimeError("duckdb: cannot create appender");
}

void RowWriter::write(const std::vector<Row>& rows) {
    if (rows.empty()) return;
    if (!impl_) open();
    for (const auto& row : rows) {
        for (std::size_t c = 0; c < columns_.size(); ++c) {
            const std::string& v = c < row.size() ? row[c] : std::string();
            duckdb_append_varchar_length(impl_->appender, v.data(), v.size());
        }
        if (duckdb_appender_end_row(impl_->appender) != DuckDBSuccess) {
            std::string msg = duckdb_appender_error(impl_->appender) ? duckdb_appender_error(impl_->appender) : "";
            throw RuntimeError("duckdb: append failed: " + msg);
        }
    }
    total_ += rows.size();
}

void RowWriter::release() {
    if (!impl_) return;
    if (impl_->appender) duckdb_appender_destroy(&impl_->appender);
    impl_->db.reset();
    remove_quiet(impl_->db_file);
    remove_quiet(impl_->db_file.string() + ".wal");
    impl_.reset();
}

bool RowWriter::close() {
    if (!impl_) {
        remove_quiet(temp_);
        return false;
    }
    if (duckdb_appender_close(impl_->appender) != DuckDBSuccess) {
        std::string msg = duckdb_appender_error(impl_->appender) ? duckdb_appender_error(impl_->appender) : "";
        release();
        throw RuntimeError("duckdb: appender close failed: " + msg);
    }
    try {
        impl_->db->exec(copy_sql("t", temp_));
    } catch (...) {
        release();
        remove_quiet(temp_);
        throw;
    }
    release();
    util::replace_file(temp_, path_);
    return true;
}

void RowWriter::abort() {
    release();
    remove_quiet(temp_);
}

bool write_rows(const fs::path& path, const std::vector<std::string>& columns, const std::vector<Row>& rows) {
    RowWriter w(path, columns);
    w.write(rows);
    return w.close();
}

void write_table(const fs::path& path, const std::vector<Column>& columns, const std::vector<NullableRow>& rows) {
    fs::create_directories(path.parent_path());
    fs::path db_file = path.string() + ".duckdb";
    remove_quiet(db_file);
    remove_quiet(db_file.string() + ".wal");
    {
        Db db(db_file.string(), true, path.parent_path());
        std::vector<std::string> defs;
        for (const auto& c : columns) defs.push_back(sql_ident(c.name) + " " + type_sql(c.type));
        db.exec("CREATE TABLE t (" + util::join(defs, ", ") + ")");
        duckdb_appender app = nullptr;
        if (duckdb_appender_create(db.con, nullptr, "t", &app) != DuckDBSuccess)
            throw RuntimeError("duckdb: cannot create appender");
        for (const auto& row : rows) {
            for (std::size_t c = 0; c < columns.size(); ++c) {
                const Cell& v = row[c];
                if (!v) duckdb_append_null(app);
                else if (columns[c].type == Type::Int32)
                    duckdb_append_int32(app, static_cast<std::int32_t>(std::stoll(*v)));
                else duckdb_append_varchar_length(app, v->data(), v->size());
            }
            duckdb_appender_end_row(app);
        }
        duckdb_appender_close(app);
        duckdb_appender_destroy(&app);
        db.exec(copy_sql("t", path));
    }
    remove_quiet(db_file);
    remove_quiet(db_file.string() + ".wal");
}

int Table::index(const std::string& name) const {
    for (std::size_t i = 0; i < columns.size(); ++i)
        if (columns[i] == name) return static_cast<int>(i);
    return -1;
}

Table read_table(const fs::path& path, const std::vector<std::string>& columns) {
    Table table;
    std::string sql = "SELECT " + select_list(path, columns) + " FROM read_parquet(" + sql_quote(path.string()) + ")";
    query_chunks(sql, [&](const std::vector<std::string>& names, duckdb_data_chunk chunk) {
        if (table.columns.empty()) table.columns = names;
        idx_t n = duckdb_data_chunk_get_size(chunk);
        std::size_t ncols = names.size();
        std::vector<duckdb_string_t*> data(ncols);
        std::vector<uint64_t*> validity(ncols);
        for (std::size_t c = 0; c < ncols; ++c) {
            auto vec = duckdb_data_chunk_get_vector(chunk, c);
            data[c] = static_cast<duckdb_string_t*>(duckdb_vector_get_data(vec));
            validity[c] = duckdb_vector_get_validity(vec);
        }
        for (idx_t r = 0; r < n; ++r) {
            NullableRow row(ncols);
            for (std::size_t c = 0; c < ncols; ++c) {
                if (validity[c] && !duckdb_validity_row_is_valid(validity[c], r)) continue;
                duckdb_string_t* s = &data[c][r];
                row[c] = std::string(duckdb_string_t_data(s), duckdb_string_t_length(*s));
            }
            table.rows.push_back(std::move(row));
        }
    });
    if (table.columns.empty()) table.columns = columns.empty() ? column_names(path) : columns;
    return table;
}

void iter_rows(const fs::path& path, std::size_t batch_rows,
               const std::function<void(const std::vector<std::string>&, const std::vector<Row>&)>& fn,
               const std::vector<std::string>& wanted) {
    std::string sql = "SELECT " + select_list(path, wanted) + " FROM read_parquet(" + sql_quote(path.string()) + ")";
    std::vector<Row> batch;
    std::vector<std::string> cols;
    query_chunks(sql, [&](const std::vector<std::string>& names, duckdb_data_chunk chunk) {
        cols = names;
        idx_t n = duckdb_data_chunk_get_size(chunk);
        std::size_t ncols = names.size();
        std::vector<duckdb_string_t*> data(ncols);
        std::vector<uint64_t*> validity(ncols);
        for (std::size_t c = 0; c < ncols; ++c) {
            auto vec = duckdb_data_chunk_get_vector(chunk, c);
            data[c] = static_cast<duckdb_string_t*>(duckdb_vector_get_data(vec));
            validity[c] = duckdb_vector_get_validity(vec);
        }
        for (idx_t r = 0; r < n; ++r) {
            Row row(ncols);
            for (std::size_t c = 0; c < ncols; ++c) {
                if (validity[c] && !duckdb_validity_row_is_valid(validity[c], r)) continue;
                duckdb_string_t* s = &data[c][r];
                row[c].assign(duckdb_string_t_data(s), duckdb_string_t_length(*s));
            }
            batch.push_back(std::move(row));
            if (batch.size() >= batch_rows) {
                fn(cols, batch);
                batch.clear();
            }
        }
    });
    if (!batch.empty()) fn(cols, batch);
}

std::vector<std::string> column_names(const fs::path& path) {
    std::vector<std::string> out;
    auto& db = reader();
    duckdb_result result;
    std::string sql = "SELECT * FROM read_parquet(" + sql_quote(path.string()) + ") LIMIT 0";
    if (duckdb_query(db.con, sql.c_str(), &result) != DuckDBSuccess) {
        std::string msg = duckdb_result_error(&result) ? duckdb_result_error(&result) : "unknown error";
        duckdb_destroy_result(&result);
        throw RuntimeError("duckdb: " + msg);
    }
    for (idx_t c = 0; c < duckdb_column_count(&result); ++c) out.emplace_back(duckdb_column_name(&result, c));
    duckdb_destroy_result(&result);
    return out;
}

std::int64_t row_count(const fs::path& path) {
    std::int64_t total = 0;
    query_chunks("SELECT CAST(num_rows AS VARCHAR) FROM parquet_file_metadata(" + sql_quote(path.string()) + ")",
                 [&](const std::vector<std::string>&, duckdb_data_chunk chunk) {
                     auto vec = duckdb_data_chunk_get_vector(chunk, 0);
                     auto* data = static_cast<duckdb_string_t*>(duckdb_vector_get_data(vec));
                     for (idx_t r = 0; r < duckdb_data_chunk_get_size(chunk); ++r)
                         total += std::stoll(std::string(duckdb_string_t_data(&data[r]), duckdb_string_t_length(data[r])));
                 });
    return total;
}

}  // namespace premarket::parquet
