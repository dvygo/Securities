//! Reading a file into the viewer's one in-memory shape: a header plus rows of
//! strings.
//!
//! Everything is stringified on the way in, matching how the Python side treats
//! these files (parquet_export writes every column as a string deliberately, so
//! a viewer inferring types here would show something the pipeline never
//! stored). Cells are truncated per-column at render time, not here.

use anyhow::{bail, Context, Result};
use std::path::Path;

pub struct Table {
    pub header: Vec<String>,
    pub rows: Vec<Vec<String>>,
    /// What the header/rows were read from, shown in the status bar.
    pub origin: String,
}

impl Table {
    pub fn load(path: &Path, limit: usize) -> Result<Table> {
        let name = path.file_name().unwrap_or_default().to_string_lossy().to_string();
        match kind(path) {
            Kind::Csv => load_csv(path, limit),
            Kind::Parquet => load_parquet(path, limit),
            Kind::Dbn => load_dbn(path, limit),
            Kind::Unknown => bail!(
                "{name}: unrecognised format. Handled: .csv, .parquet, .dbn, .dbn.zst"
            ),
        }
    }
}

enum Kind { Csv, Parquet, Dbn, Unknown }

fn kind(path: &Path) -> Kind {
    let name = path.file_name().unwrap_or_default().to_string_lossy().to_lowercase();
    if name.ends_with(".csv") {
        Kind::Csv
    } else if name.ends_with(".parquet") {
        Kind::Parquet
    } else if name.ends_with(".dbn") || name.ends_with(".dbn.zst") {
        Kind::Dbn
    } else {
        Kind::Unknown
    }
}

fn load_csv(path: &Path, limit: usize) -> Result<Table> {
    let mut reader = csv::ReaderBuilder::new()
        .flexible(true)
        .from_path(path)
        .with_context(|| format!("opening {}", path.display()))?;
    let header: Vec<String> = reader
        .headers()?
        .iter()
        .map(|h| h.trim_start_matches('\u{feff}').to_string())   // vendor CSVs are utf-8-sig
        .collect();
    let mut rows = Vec::new();
    for record in reader.records().take(limit) {
        rows.push(record?.iter().map(|c| c.to_string()).collect());
    }
    Ok(Table { header, rows, origin: format!("csv · {}", path.display()) })
}

#[cfg(not(feature = "columnar"))]
fn load_parquet(_path: &Path, _limit: usize) -> Result<Table> {
    bail!("parquet support needs the `columnar` feature (see README: it requires a C toolchain for zstd)")
}

#[cfg(not(feature = "columnar"))]
fn load_dbn(_path: &Path, _limit: usize) -> Result<Table> {
    bail!("dbn support needs the `columnar` feature (see README: it requires a C toolchain for zstd)")
}

#[cfg(feature = "columnar")]
fn load_parquet(path: &Path, limit: usize) -> Result<Table> {
    use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
    let file = std::fs::File::open(path).with_context(|| format!("opening {}", path.display()))?;
    // Row-group-at-a-time, capped at `limit`: these files run to millions of
    // rows and a viewer must not pull the whole thing in to show a screenful.
    let builder = ParquetRecordBatchReaderBuilder::try_new(file)?;
    let schema = builder.schema().clone();
    let header: Vec<String> = schema.fields().iter().map(|f| f.name().to_string()).collect();
    let reader = builder.with_batch_size(limit.min(8192)).build()?;
    let mut rows: Vec<Vec<String>> = Vec::new();
    for batch in reader {
        let batch = batch?;
        for r in 0..batch.num_rows() {
            if rows.len() >= limit { break; }
            rows.push((0..batch.num_columns())
                .map(|c| cell(batch.column(c), r))
                .collect());
        }
        if rows.len() >= limit { break; }
    }
    Ok(Table { header, rows, origin: format!("parquet · {}", path.display()) })
}

#[cfg(feature = "columnar")]
fn cell(array: &std::sync::Arc<dyn arrow::array::Array>, row: usize) -> String {
    use arrow::util::display::{ArrayFormatter, FormatOptions};
    match ArrayFormatter::try_new(array.as_ref(), &FormatOptions::default()) {
        Ok(f) => f.value(row).to_string(),
        Err(_) => String::new(),
    }
}

#[cfg(feature = "columnar")]
fn load_dbn(path: &Path, limit: usize) -> Result<Table> {
    use dbn::decode::{DbnRecordDecoder, DecodeRecordRef};
    let file = std::fs::File::open(path).with_context(|| format!("opening {}", path.display()))?;
    // .zst is sniffed from the magic bytes rather than the extension, so a
    // compressed file named .dbn still reads.
    let mut decoder = DbnRecordDecoder::with_zstd(file)?;
    let header = vec![
        "rtype".into(), "instrument_id".into(), "raw_symbol".into(),
        "instrument_class".into(), "ts_event".into(),
    ];
    let mut rows = Vec::new();
    while let Some(rec) = decoder.decode_record_ref()? {
        if rows.len() >= limit { break; }
        rows.push(describe(&rec));
    }
    Ok(Table { header, rows, origin: format!("dbn · {}", path.display()) })
}

#[cfg(feature = "columnar")]
fn describe(rec: &dbn::RecordRef) -> Vec<String> {
    use dbn::InstrumentDefMsg;
    if let Some(d) = rec.get::<InstrumentDefMsg>() {
        return vec![
            "definition".into(),
            d.hd.instrument_id.to_string(),
            d.raw_symbol().unwrap_or_default().to_string(),
            format!("{}", d.instrument_class as u8 as char),
            d.hd.ts_event.to_string(),
        ];
    }
    vec![format!("{:?}", rec.header().rtype), rec.header().instrument_id.to_string(),
         String::new(), String::new(), rec.header().ts_event.to_string()]
}
