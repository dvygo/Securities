#include "premarket/postgres.hpp"

#include <libpq-fe.h>

#include <array>
#include <charconv>
#include <memory>
#include <set>

#include "premarket/csv.hpp"
#include "premarket/log.hpp"
#include "premarket/parquet_io.hpp"
#include "premarket/plugin.hpp"
#include "premarket/util.hpp"

namespace premarket::postgres {

namespace {

const std::map<std::string, std::string> kTypes = {
    {"trade_date", "date NOT NULL DEFAULT CURRENT_DATE"},
    {"segment", "varchar(15)"},
    {"token", "int8 NOT NULL DEFAULT 0"},
    {"symbol", "varchar(255)"},
    {"expirydate", "varchar(11)"},
    {"insttype", "varchar(255)"},
    {"optiontype", "varchar(255)"},
    {"strikeprice", "int8"},
    {"lotmultiple", "float8"},
    {"lotsize", "int4"},
    {"ticksize", "float8 DEFAULT 0"},
    {"name", "varchar(255)"},
    {"series", "varchar(255)"},
    {"divisor", "int4 NOT NULL DEFAULT 0"},
    {"exch", "varchar(10)"},
    {"fullname", "varchar(100)"},
    {"freeze_qty", "int4"},
};
const std::map<std::string, std::int64_t> kFeedPriceScale = {{"XCME", 100}, {"XNAS", 100}, {"XCBO", 10000}};
const char* kTemp = "plugin_upsert_staging";

struct Conn {
    PGconn* c = nullptr;
    explicit Conn(const std::string& url) {
        c = PQconnectdb(url.c_str());
        if (PQstatus(c) != CONNECTION_OK) {
            std::string msg = PQerrorMessage(c);
            PQfinish(c);
            c = nullptr;
            throw RuntimeError("postgres: " + util::strip(msg));
        }
    }
    ~Conn() {
        if (c) PQfinish(c);
    }
    void exec(const std::string& sql) {
        PGresult* r = PQexec(c, sql.c_str());
        auto status = PQresultStatus(r);
        std::string msg = PQresultErrorMessage(r);
        PQclear(r);
        if (status != PGRES_COMMAND_OK && status != PGRES_TUPLES_OK) throw RuntimeError("postgres: " + util::strip(msg));
    }
    std::int64_t exec_count(const std::string& sql) {
        PGresult* r = PQexec(c, sql.c_str());
        auto status = PQresultStatus(r);
        std::string msg = PQresultErrorMessage(r);
        std::string tuples = PQcmdTuples(r);
        PQclear(r);
        if (status != PGRES_COMMAND_OK && status != PGRES_TUPLES_OK) throw RuntimeError("postgres: " + util::strip(msg));
        return tuples.empty() ? 0 : std::stoll(tuples);
    }
    bool table_exists(const std::string& schema, const std::string& table) {
        std::string qualified = "\"" + schema + "\".\"" + table + "\"";
        const char* params[1] = {qualified.c_str()};
        PGresult* r = PQexecParams(c, "SELECT to_regclass($1)", 1, nullptr, params, nullptr, nullptr, 0);
        if (PQresultStatus(r) != PGRES_TUPLES_OK) {
            std::string msg = PQresultErrorMessage(r);
            PQclear(r);
            throw RuntimeError("postgres: " + util::strip(msg));
        }
        bool exists = !PQgetisnull(r, 0, 0);
        PQclear(r);
        return exists;
    }
};

std::int64_t whole(const std::string& v) {
    auto f = util::parse_int_truncating(v);
    return f ? *f : 0;
}

std::string shortest(double x) {
    std::array<char, 64> buf{};
    auto [end, ec] = std::to_chars(buf.data(), buf.data() + buf.size(), x);
    return std::string(buf.data(), end);
}

std::string q(const std::string& ident) { return "\"" + util::replace_all(ident, "\"", "\"\"") + "\""; }

}  // namespace

std::string create_table_sql(const std::string& schema, const std::string& table) {
    std::vector<std::string> cols;
    for (const auto& c : plugin::PLUGIN_COLUMNS) {
        auto it = kTypes.find(c);
        if (it == kTypes.end()) throw ValueError("No Postgres type for plugin column(s): " + c);
        cols.push_back("    " + q(c) + " " + it->second);
    }
    return "CREATE TABLE IF NOT EXISTS " + q(schema) + "." + q(table) + " (\n" + util::join(cols, ",\n") +
           ",\n    PRIMARY KEY (\"token\", \"trade_date\")\n)";
}

std::string upsert_sql(const std::string& schema, const std::string& table) {
    std::vector<std::string> cols, updates;
    for (const auto& c : plugin::PLUGIN_COLUMNS) {
        cols.push_back(q(c));
        if (c != "token" && c != "trade_date") updates.push_back(q(c) + " = EXCLUDED." + q(c));
    }
    std::string list = util::join(cols, ", ");
    return "INSERT INTO " + q(schema) + "." + q(table) + " (" + list + ") SELECT DISTINCT ON (\"token\", \"trade_date\") " +
           list + " FROM " + q(kTemp) + " ON CONFLICT (\"token\", \"trade_date\") DO UPDATE SET " + util::join(updates, ", ");
}

Scaled to_feed_scale(const std::map<std::string, std::string>& row) {
    Scaled out{row, false, false};
    auto exch = row.find("exch");
    auto scale_it = exch == row.end() ? kFeedPriceScale.end() : kFeedPriceScale.find(exch->second);
    std::int64_t divisor = whole(row.count("divisor") ? row.at("divisor") : "");
    if (scale_it == kFeedPriceScale.end() || divisor <= scale_it->second || divisor % scale_it->second) return out;
    std::int64_t scale = scale_it->second;
    std::int64_t factor = divisor / scale;
    out.changed = true;
    out.row["divisor"] = std::to_string(scale);
    std::int64_t strike = whole(row.count("strikeprice") ? row.at("strikeprice") : "");
    if (strike > 0) {
        out.inexact = strike % factor != 0;
        out.row["strikeprice"] = std::to_string((strike + factor / 2) / factor);
    }
    std::int64_t tick = whole(row.count("ticksize") ? row.at("ticksize") : "");
    if (tick > 1)
        out.row["ticksize"] = tick % factor == 0 ? std::to_string(tick / factor)
                                                 : shortest(static_cast<double>(tick) / static_cast<double>(factor));
    return out;
}

bool spot_fullname(std::map<std::string, std::string>& row) {
    if (row["exch"] != "XNAS" || row["insttype"] != "EQUITY") return false;
    std::string ticker = util::strip(!row["symbol"].empty() ? row["symbol"] : row["fullname"]);
    if (ticker.empty() || util::starts_with(row["fullname"], "SPOT ")) return false;
    row["fullname"] = "SPOT " + ticker;
    return true;
}

std::vector<Result> push(const Config& cfg, const std::vector<std::filesystem::path>& files) {
    if (cfg.database_url.empty()) throw ValueError("postgres-plugin sink: database_url is not set");
    if (cfg.schema.empty() || cfg.table.empty()) throw ValueError("postgres-plugin sink: schema and table must both be set");
    std::vector<Result> results;
    if (files.empty()) {
        log::out("  No plugin rows to push to " + cfg.schema + "." + cfg.table);
        return results;
    }
    log::out("  Appending to " + cfg.schema + "." + cfg.table + "...");
    Conn conn(cfg.database_url);
    if (!conn.table_exists(cfg.schema, cfg.table)) {
        if (!cfg.create_table)
            throw RuntimeError("relation \"" + cfg.schema + "." + cfg.table + "\" does not exist. Check the sink's schema/table -- "
                               "this is the error that catches a typo. If the database is genuinely new and the table "
                               "should be created, set create_table = 1 in the sink file.");
        log::out("    Creating " + cfg.schema + "." + cfg.table + " (create_table = 1)");
        conn.exec("BEGIN");
        conn.exec("CREATE SCHEMA IF NOT EXISTS " + q(cfg.schema));
        conn.exec(create_table_sql(cfg.schema, cfg.table));
        conn.exec("COMMIT");
    }
    std::vector<std::string> quoted;
    for (const auto& c : plugin::PLUGIN_COLUMNS) quoted.push_back(q(c));
    for (const auto& path : files) {
        std::int64_t read = 0, dropped = 0, rescaled = 0, inexact = 0, spot_named = 0;
        conn.exec("BEGIN");
        conn.exec("CREATE TEMP TABLE " + q(kTemp) + " (LIKE " + q(cfg.schema) + "." + q(cfg.table) + ") ON COMMIT DROP");
        std::string copy = "COPY " + q(kTemp) + " (" + util::join(quoted, ", ") + ") FROM STDIN WITH (FORMAT csv, HEADER true)";
        PGresult* r = PQexec(conn.c, copy.c_str());
        if (PQresultStatus(r) != PGRES_COPY_IN) {
            std::string msg = PQresultErrorMessage(r);
            PQclear(r);
            conn.exec("ROLLBACK");
            throw RuntimeError("postgres: " + util::strip(msg));
        }
        PQclear(r);
        std::string header = csv::format_line(plugin::PLUGIN_COLUMNS) + "\n";
        PQputCopyData(conn.c, header.data(), static_cast<int>(header.size()));
        parquet::iter_rows(path, parquet::CHUNK_ROWS, [&](const std::vector<std::string>& columns, const std::vector<parquet::Row>& rows) {
            std::string chunk;
            for (const auto& raw : rows) {
                std::map<std::string, std::string> row;
                for (std::size_t c = 0; c < columns.size(); ++c) row[columns[c]] = raw[c];
                if (row["exch"] == "XCBO" && row["insttype"] == "SPOT") {
                    ++dropped;
                    continue;
                }
                auto scaled = to_feed_scale(row);
                rescaled += scaled.changed;
                inexact += scaled.inexact;
                spot_named += spot_fullname(scaled.row);
                std::vector<std::string> values;
                for (const auto& c : plugin::PLUGIN_COLUMNS) values.push_back(scaled.row[c]);
                chunk += csv::format_line(values) + "\n";
                ++read;
            }
            if (!chunk.empty() && PQputCopyData(conn.c, chunk.data(), static_cast<int>(chunk.size())) != 1)
                throw RuntimeError(std::string("postgres: ") + PQerrorMessage(conn.c));
        });
        if (PQputCopyEnd(conn.c, nullptr) != 1) throw RuntimeError(std::string("postgres: ") + PQerrorMessage(conn.c));
        PGresult* done = PQgetResult(conn.c);
        auto status = PQresultStatus(done);
        std::string msg = PQresultErrorMessage(done);
        PQclear(done);
        while (PGresult* extra = PQgetResult(conn.c)) PQclear(extra);
        if (status != PGRES_COMMAND_OK) {
            conn.exec("ROLLBACK");
            throw RuntimeError("postgres: " + util::strip(msg));
        }
        if (read == 0) {
            conn.exec("ROLLBACK");
            results.push_back({path, 0, 0});
            log::out("    Upserted 0 rows from " + path.filename().string());
            continue;
        }
        std::int64_t affected = conn.exec_count(upsert_sql(cfg.schema, cfg.table));
        conn.exec("COMMIT");
        std::string note = read == affected ? "" : " (" + std::to_string(read - affected) + " duplicate key(s) collapsed)";
        if (dropped) note += " (" + std::to_string(dropped) + " XCBO SPOT row(s) not pushed)";
        if (rescaled) {
            note += " (" + std::to_string(rescaled) + " row(s) in feed price units";
            note += inexact ? ", " + std::to_string(inexact) + " strike(s) rounded)" : ")";
        }
        if (spot_named) note += " (" + std::to_string(spot_named) + " spot fullname(s) prefixed 'SPOT ')";
        log::out("    Upserted " + std::to_string(affected) + " rows from " + path.filename().string() + note);
        results.push_back({path, read, affected});
    }
    return results;
}

}  // namespace premarket::postgres
