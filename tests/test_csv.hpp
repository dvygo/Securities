#pragma once
// premarket/csv.hpp -- RFC 4180 CSV, the dialect of the vendor files (Fyers, NSE) and of
// --csv exports: fields separated by ',', a field holding ',', '"' or a line break is
// quoted, and '"' inside a quoted field is doubled. parse_records() keeps a line break
// inside quotes as part of the field, and accepts both \n and \r\n line ends;
// format_line() quotes only the fields that need it.
// An empty line is a record with no fields, as Python's csv module read it in 7.x.
