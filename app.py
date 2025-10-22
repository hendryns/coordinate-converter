import streamlit as st
import pandas as pd
import geopandas as gpd
from pyproj import Transformer, CRS
import re
import io
import zipfile
import os
import uuid
import folium  # Baru: Untuk membuat peta
import streamlit_folium as stf  # Baru: Untuk menampilkan peta Folium di Streamlit
import utm  # Baru: Untuk konversi DD ke UTM otomatis
# Pustaka 'openpyxl' juga diperlukan (diinstal via pip) agar Pandas bisa menulis .xlsx

# --- Pengaturan Halaman Streamlit ---
st.set_page_config(layout="wide", page_title="Konverter Koordinat Pro")

# --- Fungsi Helper (Logika Inti) ---

def parse_dms(dms_str):
    """
    Mengurai string DMS (Derajat Menit Detik) ke Decimal Degrees (float).
    Fleksibel menangani format N/S/E/W dan LU/LS/BT/BB.
    """
    if not isinstance(dms_str, str):
        return pd.NA
        
    s = dms_str.upper().strip()
    
    # Normalisasi singkatan Indonesia ke Bahasa Inggris
    s = s.replace("LU", "N").replace("LS", "S")
    s = s.replace("UTARA", "N").replace("SELATAN", "S")
    s = s.replace("BT", "E").replace("BB", "W")
    s = s.replace("TIMUR", "E").replace("BARAT", "W")

    is_lat = 'N' in s or 'S' in s
    is_lon = 'E' in s or 'W' in s
    
    s_cleaned = re.sub(r"[^0-9\s.-]", "", s).strip()
    
    try:
        parts = re.split(r'\s+', s_cleaned)
        
        deg = float(parts[0]) if len(parts) > 0 else 0
        mnt = float(parts[1]) if len(parts) > 1 else 0
        sec = float(parts[2]) if len(parts) > 2 else 0
        
        dd = abs(deg) + mnt/60 + sec/3600
        
        if 'S' in s or 'W' in s or deg < 0:
            dd = -dd
            
        return dd
        
    except Exception as e:
        st.error(f"Error parsing DMS '{dms_str}': {e}")
        return pd.NA

def dd_to_dms(dd, is_lat=True):
    """
    Mengonversi Decimal Degree (float) kembali ke format string DMS.
    """
    try:
        dd = float(dd)
        is_negative = dd < 0
        dd = abs(dd)
        
        d = int(dd)
        m_float = (dd - d) * 60
        m = int(m_float)
        s = round((m_float - m) * 60, 4)
        
        if is_lat:
            direction = "S (LS)" if is_negative else "N (LU)"
        else:
            direction = "W (BB)" if is_negative else "E (BT)"
            
        return f"{d}° {m}' {s}\" {direction}"
        
    except Exception:
        return pd.NA

def get_input_epsg_code(zone_num, zone_hemi, datum):
    """
    (Diperbarui) Mendapatkan kode EPSG input berdasarkan Datum, Zona, dan Hemisphere.
    Ini krusial untuk konversi yang akurat dari datum non-WGS84.
    """
    zone_hemi = zone_hemi.upper()
    
    if datum == "WGS84":
        if not 1 <= zone_num <= 60:
            raise ValueError("Nomor zona UTM WGS84 harus antara 1 dan 60")
        if zone_hemi == 'S':
            return f"327{zone_num:02d}"  # WGS 84 / UTM Southern Hemisphere
        else: # 'N'
            return f"326{zone_num:02d}"  # WGS 84 / UTM Northern Hemisphere
            
    elif datum == "DGN95":
        # Peta EPSG manual untuk DGN95 / UTM (umum di Indonesia)
        # Rentang zona Indonesia: 46-54
        epsg_map_dgn95_n = {
            46: "23846", 47: "23847", 48: "23848", 49: "23849", 50: "23850",
            51: "23851", 52: "23852", 53: "23853", 54: "23854"
        }
        epsg_map_dgn95_s = {
            46: "23896", 47: "23897", 48: "23898", 49: "23899", 50: "23890", # Anomali di 50S
            51: "23891", 52: "23892", 53: "23893", 54: "23894"
        }
        
        if zone_hemi == 'N':
            if zone_num not in epsg_map_dgn95_n:
                raise ValueError(f"Zona {zone_num}N tidak memiliki mapping EPSG DGN95 yang valid.")
            return epsg_map_dgn95_n[zone_num]
        else: # 'S'
            if zone_num not in epsg_map_dgn95_s:
                raise ValueError(f"Zona {zone_num}S tidak memiliki mapping EPSG DGN95 yang valid.")
            return epsg_map_dgn95_s[zone_num]
            
    else:
        raise ValueError(f"Datum '{datum}' tidak didukung.")

def convert_dd_to_auto_utm(row):
    """
    (Baru) Menggunakan pustaka 'utm' untuk secara otomatis menemukan zona,
    easting, dan northing yang benar dari lat/lon.
    """
    try:
        # utm.from_latlon mengembalikan (Easting, Northing, Zone Number, Zone Letter)
        easting, northing, zone_num, zone_letter = utm.from_latlon(row['lat_dd'], row['lon_dd'])
        
        # Tentukan Hemisphere dari Zone Letter (misal 'M' itu S, 'N' itu N)
        zone_hemi = 'N' if zone_letter.upper() >= 'N' else 'S'
        
        return easting, northing, f"{zone_num}{zone_hemi}"
    except Exception:
        return pd.NA, pd.NA, pd.NA

def process_coordinates(df_input, input_crs_name, input_datum, input_zone_str):
    """
    (Diperbarui) Fungsi pemroses utama.
    Alur: [INPUT] -> [DD WGS84] -> [SEMUA FORMAT OUTPUT]
    """
    df = df_input.copy()
    
    # === LANGKAH 1: Konversi semua format input ke DD WGS84 (EPSG:4326) ===
    
    if input_crs_name == "Decimal Degrees (DD)":
        # Jika input adalah DD, kita asumsikan WGS84
        # (Konversi DD antar datum non-trivial, jadi kita tetapkan WGS84 sebagai standar DD)
        df['lon_dd'] = pd.to_numeric(df['x'], errors='coerce')
        df['lat_dd'] = pd.to_numeric(df['y'], errors='coerce')
        if input_datum != "WGS84":
            st.warning("Input Decimal Degrees diiasumsikan sebagai WGS84. Pengaturan Datum diabaikan.")
        
    elif input_crs_name == "Geografis (DMS)":
        # Sama seperti DD, DMS diiasumsikan WGS84
        df['lon_dd'] = df['x'].apply(parse_dms)
        df['lat_dd'] = df['y'].apply(parse_dms)
        if input_datum != "WGS84":
            st.warning("Input Geografis (DMS) diiasumsikan sebagai WGS84. Pengaturan Datum diabaikan.")
        
    elif input_crs_name == "UTM":
        # Ini adalah bagian yang menggunakan Datum
        try:
            zone_num = int(input_zone_str[:-1])
            zone_hemi = input_zone_str[-1].upper()
            
            # Dapatkan kode EPSG sumber (bisa WGS84 atau DGN95)
            source_epsg = get_input_epsg_code(zone_num, zone_hemi, input_datum)
            
            # Target kita *selalu* DD WGS84 (EPSG:4326)
            crs_source = CRS(f"EPSG:{source_epsg}")
            crs_target = CRS("EPSG:4326")
            
            transformer = Transformer.from_crs(crs_source, crs_target, always_xy=True)
            
            easting = pd.to_numeric(df['x'], errors='coerce')
            northing = pd.to_numeric(df['y'], errors='coerce')
            
            df['lon_dd'], df['lat_dd'] = transformer.transform(
                easting.values, 
                northing.values
            )
        except Exception as e:
            st.error(f"Error Konversi UTM ke DD: {e}")
            return None

    # Hapus baris yang gagal di-parse
    df = df.dropna(subset=['lon_dd', 'lat_dd'])
    if df.empty:
        st.warning("Tidak ada data valid yang dapat diproses setelah parsing.")
        return None

    # === LANGKAH 2: Konversi dari DD WGS84 ke semua format output ===
    
    # 2a. Konversi ke DMS (WGS84)
    df['lon_dms'] = df['lon_dd'].apply(lambda d: dd_to_dms(d, is_lat=False))
    df['lat_dms'] = df['lat_dd'].apply(lambda d: dd_to_dms(d, is_lat=True))

    # 2b. (Diperbarui) Konversi ke UTM Otomatis (WGS84)
    # Terapkan fungsi 'convert_dd_to_auto_utm' ke setiap baris
    df[['easting_utm', 'northing_utm', 'zone_utm']] = df.apply(
        convert_dd_to_auto_utm, 
        axis=1, 
        result_type='expand'
    )

    # === LANGKAH 3: Finalisasi DataFrame ===
    
    # Susun ulang kolom agar rapi
    output_columns = [
        'lat_dd', 'lon_dd', 
        'lat_dms', 'lon_dms', 
        'easting_utm', 'northing_utm', 'zone_utm'
    ]
    original_cols = [col for col in df_input.columns if col not in ['x', 'y']]
    df_final = df[original_cols + output_columns]
    
    return df_final

# --- Antarmuka Streamlit (UI) ---

st.title("🌐 Konverter Sistem Koordinat Pro")
st.write("Konversi antara DD, UTM, dan DMS, kini dengan dukungan Datum, deteksi zona otomatis, dan visualisasi peta.")

# Inisialisasi variabel
df_input = None
input_zone = None
input_datum = "WGS84" # Default

# --- KOLOM PENGATURAN ---
col_settings_1, col_settings_2 = st.columns(2)

with col_settings_1:
    st.header("1. Metode Input")
    input_method = st.radio(
        "Pilih cara memasukkan data:",
        ("Manual", "Unggah File CSV"),
        horizontal=True
    )
    
    st.header("2. Sistem Koordinat Input")
    input_crs_name = st.selectbox(
        "Pilih sistem koordinat data input Anda:",
        ("Decimal Degrees (DD)", "UTM", "Geografis (DMS)")
    )

with col_settings_2:
    st.header("3. Pengaturan Input Lanjutan")
    
    # (Diperbarui) Pengaturan Datum dan Zona hanya muncul jika input_crs == "UTM"
    if input_crs_name == "UTM":
        st.subheader("Datum & Zona Input")
        input_datum = st.selectbox(
            "Datum Input:",
            ("WGS84", "DGN95"),
            help="Pilih datum data UTM Anda. DGN95 umum digunakan di Indonesia."
        )
        
        help_utm = "Pilih zona dan hemisphere (N/S) dari data UTM *input* Anda."
        col_z_in_1, col_z_in_2 = st.columns(2)
        
        # Batasi pilihan zona jika DGN95 dipilih (rentang Indonesia)
        min_zone, max_zone = (46, 54) if input_datum == "DGN95" else (1, 60)
        default_zone = 48 if min_zone <= 48 <= max_zone else min_zone
        
        in_zone_num = col_z_in_1.number_input("Nomor Zona", min_zone, max_zone, default_zone, key="in_num", help=help_utm)
        in_zone_hemi = col_z_in_2.selectbox("Hemisphere", ("S", "N"), key="in_hemi", help=help_utm)
        input_zone = f"{in_zone_num}{in_zone_hemi}"
        
        if input_datum == "DGN95":
            st.info(f"Input diatur ke: **{input_datum} / UTM Zona {input_zone}**")
        else:
            st.info(f"Input diatur ke: **{input_datum} / UTM Zona {input_zone}**")
    else:
        st.info("Datum untuk input DD dan DMS diasumsikan WGS84.")

# (Dihapus) Bagian "Zona UTM Output" tidak diperlukan lagi.

# --- INPUT DATA ---
st.divider()
st.header("4. Masukkan Data Koordinat")

if input_method == "Manual":
    col_man_1, col_man_2 = st.columns(2)
    
    if input_crs_name == "Decimal Degrees (DD)":
        x_label, y_label = "Longitude (X)", "Latitude (Y)"
        x_placeholder, y_placeholder = "Contoh: 106.827", "Contoh: -6.175"
    elif input_crs_name == "UTM":
        x_label, y_label = f"Easting (X) - {input_datum}", f"Northing (Y) - {input_datum}"
        x_placeholder, y_placeholder = "Contoh: 703000", "Contoh: 9317000"
    else: # Geografis (DMS)
        x_label, y_label = "Longitude (X)", "Latitude (Y)"
        x_placeholder, y_placeholder = "Contoh: 106 49 37 BT", "Contoh: 6 10 30 LS"

    x_input = col_man_1.text_input(x_label, placeholder=x_placeholder)
    y_input = col_man_2.text_input(y_label, placeholder=y_placeholder)
    
    if x_input and y_input:
        df_input = pd.DataFrame([{'x': x_input, 'y': y_input}])

else: # Unggah File CSV
    uploaded_file = st.file_uploader(
        "Unggah file .csv", 
        type=["csv"],
        help="Pastikan file CSV memiliki kolom 'x' dan 'y' (huruf besar/kecil tidak masalah)."
    )
    with st.expander("ℹ️ Klik untuk melihat contoh struktur file .csv"):
        st.write("""
        File CSV Anda **harus** memiliki kolom bernama `x` dan `y`.
        -   Kolom `x` harus berisi data Longitude / Easting.
        -   Kolom `y` harus berisi data Latitude / Northing.
        
        Huruf besar/kecil pada nama kolom tidak masalah (`x`, `X`, `Y`, `y` semua diterima).
        Kolom tambahan lain (seperti `id`, `nama_lokasi`, dll.) akan tetap dipertahankan dalam hasil konversi.
        """)
        
        # Buat data contoh
        contoh_data = {
            'x': ['106.827153', '106.828111', '703000.12'],
            'y': ['-6.175392', '-6.176333', '9317000.45'],
            'nama_lokasi': ['Monas', 'Stasiun Gambir', 'Titik UTM']
        }
        df_contoh = pd.DataFrame(contoh_data)
        
        st.write("**Contoh tampilan di Excel/Spreadsheet:**")
        st.dataframe(df_contoh, use_container_width=True)
        
        st.write("**Contoh tampilan di file .csv (teks mentah):**")
        st.code(
            "x,y,nama_lokasi\n"
            "106.827153,-6.175392,Monas\n"
            "106.828111,-6.176333,Stasiun Gambir\n"
            "703000.12,9317000.45,Titik UTM",
            language="csv"
        )
    if uploaded_file:
        try:
            df_input = pd.read_csv(uploaded_file)
            df_input.columns = df_input.columns.str.lower()
            if 'x' not in df_input.columns or 'y' not in df_input.columns:
                st.error("File CSV harus memiliki kolom 'x' dan 'y'.")
                df_input = None
            else:
                st.write("Data terunggah (5 baris pertama):")
                st.dataframe(df_input.head())
        except Exception as e:
            st.error(f"Gagal membaca file: {e}")
            df_input = None

# --- PROSES DAN OUTPUT ---
st.divider()
st.header("5. Hasil Konversi")

# (Diperbarui) Menambahkan opsi CSV dan Excel
output_formats = st.multiselect(
    "Pilih format file untuk diunduh:",
    ["CSV", "Excel (.xlsx)", "GeoJSON", "Shapefile (.zip)"]
)

if st.button("🚀 Konversi Sekarang", type="primary", use_container_width=True):
    if df_input is None or df_input.empty:
        st.warning("Silakan masukkan data (manual atau unggah file) terlebih dahulu.")
    elif input_crs_name == "UTM" and not input_zone:
        st.warning("Silakan pilih Zona UTM Input pada langkah 3.")
    else:
        with st.spinner("Mengonversi koordinat..."):
            try:
                # Panggil fungsi pemroses utama
                df_result = process_coordinates(df_input, input_crs_name, input_datum, input_zone)
                
                if df_result is not None and not df_result.empty:
                    st.success("Konversi Selesai!")
                    st.dataframe(df_result) # Tampilkan tabel hasil
                    
                    # --- (Baru) Opsi Download ---
                    # Siapkan 4 kolom untuk tombol download
                    st.subheader("Unduh Hasil")
                    col_dl_1, col_dl_2, col_dl_3, col_dl_4 = st.columns(4)

                    # (Baru) Download CSV
                    if "CSV" in output_formats:
                        csv_data = df_result.to_csv(index=False).encode('utf-8')
                        col_dl_1.download_button(
                            label="📥 Download CSV",
                            data=csv_data,
                            file_name="konversi_koordinat.csv",
                            mime="text/csv",
                            use_container_width=True
                        )

                    # (Baru) Download Excel
                    if "Excel (.xlsx)" in output_formats:
                        excel_buffer = io.BytesIO()
                        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                            df_result.to_excel(writer, index=False, sheet_name='Konversi')
                        excel_data = excel_buffer.getvalue()
                        col_dl_2.download_button(
                            label="📥 Download Excel",
                            data=excel_data,
                            file_name="konversi_koordinat.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True
                        )
                    
                    # Buat GeoDataFrame untuk ekspor GeoJSON & Shapefile
                    gdf = gpd.GeoDataFrame(
                        df_result,
                        geometry=gpd.points_from_xy(df_result.lon_dd, df_result.lat_dd),
                        crs="EPSG:4326" # Selalu WGS84
                    )
                    
                    # Download GeoJSON
                    if "GeoJSON" in output_formats:
                        geojson_data = gdf.to_json()
                        col_dl_3.download_button(
                            label="📥 Download GeoJSON",
                            data=geojson_data,
                            file_name="konversi_koordinat.geojson",
                            mime="application/json",
                            use_container_width=True
                        )
                            
                    # Download Shapefile (.zip)
                    if "Shapefile (.zip)" in output_formats:
                        st.warning("Catatan: Nama kolom di Shapefile akan dipotong (maks 10 karakter) karena batasan format `.dbf`.")
                        zip_buffer = io.BytesIO()
                        
                        temp_dir = f"temp_shapefiles_{uuid.uuid4()}"
                        os.makedirs(temp_dir, exist_ok=True)
                        shapefile_path = os.path.join(temp_dir, "konversi_shp")

                        try:
                            # Tulis GeoDataFrame ke file
                            gdf.to_file(f"{shapefile_path}.shp", driver="ESRI Shapefile")

                            # Zip semua file komponen
                            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                                for ext in ['.shp', '.shx', '.dbf', '.prj', '.cpg']:
                                    file_to_zip = f"{shapefile_path}{ext}"
                                    if os.path.exists(file_to_zip):
                                        arcname = f"konversi_koordinat{ext}"
                                        zf.write(file_to_zip, arcname=arcname)
                                        os.remove(file_to_zip) # Hapus file setelah di-zip
                            
                            os.rmdir(temp_dir) # Hapus direktori temp

                            col_dl_4.download_button(
                                label="📥 Download Shapefile (.zip)",
                                data=zip_buffer.getvalue(),
                                file_name="konversi_koordinat.zip",
                                mime="application/zip",
                                use_container_width=True
                            )
                        except Exception as e:
                            col_dl_4.error(f"Gagal membuat Shapefile: {e}")
                            # Bersihkan jika gagal
                            if os.path.exists(temp_dir):
                                for f in os.listdir(temp_dir):
                                    os.remove(os.path.join(temp_dir, f))
                                os.rmdir(temp_dir)
                    
                    # --- (Baru) Visualisasi Peta ---
                    st.divider()
                    st.header("6. Visualisasi Peta")
                    
                    if not gdf.empty:
                        try:
                            # Tentukan pusat peta dari rata-rata lat/lon
                            map_center = [gdf.geometry.y.mean(), gdf.geometry.x.mean()]
                            
                            # Buat peta Folium
                            m = folium.Map(location=map_center, zoom_start=10, tiles="CartoDB positron")
                            
                            # Tambahkan popup untuk setiap titik
                            for _, row in gdf.iterrows():
                                # Buat konten popup dari semua data di baris
                                popup_html = "<h4>Detail Titik</h4>"
                                popup_html += "<table>"
                                # Ambil semua kolom kecuali 'geometry'
                                for col in df_result.columns:
                                    popup_html += f"<tr><td style='padding-right:10px'><strong>{col}:</strong></td><td>{row[col]}</td></tr>"
                                popup_html += "</table>"
                                
                                iframe = folium.IFrame(popup_html, width=300, height=200)
                                popup = folium.Popup(iframe, max_width=300)
                                
                                folium.Marker(
                                    [row.geometry.y, row.geometry.x],
                                    popup=popup
                                ).add_to(m)
                            
                            # Sesuaikan batas peta agar mencakup semua titik
                            m.fit_bounds(m.get_bounds())
                            
                            # Tampilkan peta di Streamlit
                            stf.folium_static(m, width=None, height=500)
                            
                        except Exception as e:
                            st.error(f"Gagal membuat peta visualisasi: {e}")

                else:
                    st.error("Gagal memproses data. Periksa kembali input Anda.")
                    
            except Exception as e:
                st.error(f"Terjadi kesalahan besar saat pemrosesan: {e}")
                import traceback
                st.code(traceback.format_exc())