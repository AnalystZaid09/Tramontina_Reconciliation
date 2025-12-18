import streamlit as st
import pandas as pd
import re
import os
import io
from dotenv import load_dotenv
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest

# --- 1. Load Hidden Credentials ---
load_dotenv()
ENDPOINT = os.getenv("AZURE_ENDPOINT")
KEY = os.getenv("AZURE_KEY")

# --- 2. Logic Functions ---

def clean_num_strict(field):
    """Extracts absolute numeric values and removes negative signs."""
    if not field:
        return 0.0
    if hasattr(field, 'value_number') and field.value_number is not None:
        return abs(float(field.value_number))
    if hasattr(field, 'value_currency') and field.value_currency:
        return abs(float(field.value_currency.amount))
    
    content = getattr(field, 'content', '0')
    cleaned = re.sub(r'[^0-9.]', '', str(content))
    try:
        return float(cleaned) if cleaned else 0.0
    except:
        return 0.0

def extract_gross_total_qty(full_content):
    """Regex to find total quantity in the invoice text."""
    if not full_content:
        return 0.0
    match = re.search(r"Gross\s+Total\s*[:\-]?\s*(\d+)", full_content, re.IGNORECASE)
    if match:
        return float(match.group(1))
    match_alt = re.search(r"Total\s+Qty\s*[:\-]?\s*(\d+)", full_content, re.IGNORECASE)
    return float(match_alt.group(1)) if match_alt else 0.0

def load_and_clean_excel(file):
    """Cleans Excel data using Material Code and currency logic."""
    raw_df = pd.read_excel(file, header=None)
    header_row_idx = raw_df[raw_df.apply(lambda r: r.astype(str).str.contains('SKU').any(), axis=1)].index[0]
    
    df = pd.read_excel(file, header=header_row_idx)
    df.columns = df.columns.astype(str).str.strip()

    cleaned_items = pd.DataFrame()
    cleaned_items['Material Code'] = df.iloc[:, 0].astype(str).str.replace('TR-', '', regex=False).str.strip()
    
    def clean_currency(value):
        if pd.isna(value): return 0.0
        cleaned = re.sub(r'[^0-9.]', '', str(value))
        try: return float(cleaned)
        except: return 0.0

    cleaned_items['Qty_EXCEL'] = df.iloc[:, 4].apply(clean_currency)
    cleaned_items['Tax_EXCEL'] = df.iloc[:, 10].apply(clean_currency)
    cleaned_items['Total_EXCEL'] = df.iloc[:, 11].apply(clean_currency)
    return cleaned_items[cleaned_items['Material Code'] != 'nan'].reset_index(drop=True)

def extract_pdf_data(pdf_file, excel_material_codes):
    """
    Azure extraction with Cross-Reference Filter to ignore HSN tables.
    """
    client = DocumentIntelligenceClient(ENDPOINT, AzureKeyCredential(KEY))
    poller = client.begin_analyze_document("prebuilt-invoice", AnalyzeDocumentRequest(bytes_source=pdf_file.read()))
    result = poller.result()
    
    all_line_items = []
    summary = {"Gross_Total_Qty": 0.0, "Total_Tax_Footer": 0.0, "Grand_Total_Footer": 0.0}
    full_text = result.content

    for invoice in result.documents:
        items = invoice.fields.get("Items")
        if items and items.value_array:
            for item in items.value_array:
                val = item.value_object
                p_code_field = val.get("ProductCode")
                
                if not p_code_field or not p_code_field.content:
                    continue
                
                m_code = p_code_field.content.strip()
                amt = clean_num_strict(val.get("Amount"))
                
                # --- CROSS-REFERENCE FILTER ---
                # Only keep the row if the Material Code exists in the Excel list
                if m_code not in excel_material_codes.values:
                    continue
                
                if "total" in m_code.lower() or amt == 0:
                    continue

                all_line_items.append({
                    "Material Code": m_code,
                    "Total_PDF": amt
                })
        
        summary["Total_Tax_Footer"] = clean_num_strict(invoice.fields.get("TotalTax"))
        summary["Grand_Total_Footer"] = clean_num_strict(invoice.fields.get("InvoiceTotal"))

    summary["Gross_Total_Qty"] = extract_gross_total_qty(full_text)
    df = pd.DataFrame(all_line_items).drop_duplicates().reset_index(drop=True)
    return df, summary

# --- 3. Streamlit UI ---
st.set_page_config(page_title="Invoice Recon", layout="wide")
st.title("📊 Tramontina Reconciliation System")

with st.sidebar:
    st.header("Settings")
    tolerance = st.slider("Select Amount Tolerance (₹)", 0.0, 50.0, 10.0)

st.header("Upload Files")
col1, col2 = st.columns(2)
with col1:
    pdf_upload = st.file_uploader("Upload PDF Invoice", type=['pdf'])
with col2:
    excel_upload = st.file_uploader("Upload Excel Sheet", type=['xlsx'])

if pdf_upload and excel_upload:
    if st.button("🔍 Start Reconciliation", type="primary"):
        with st.spinner("Processing..."):
            # 1. Process Excel first to get the filter list
            excel_df = load_and_clean_excel(excel_upload)
            valid_codes = excel_df['Material Code']
            
            # 2. Extract PDF using the filter list
            pdf_df, pdf_summary = extract_pdf_data(pdf_upload, valid_codes)

            # 3. Merge and Compare
            comp_df = pd.merge(pdf_df, excel_df[['Material Code', 'Total_EXCEL']], on="Material Code", how="outer").fillna(0)
            comp_df['Status'] = comp_df.apply(lambda x: "✅ Match" if abs(x['Total_PDF'] - x['Total_EXCEL']) <= tolerance else "❌ Mismatch", axis=1)

            # Grand Totals Logic
            summary_results = pd.DataFrame([
                {"Metric": "Total Quantity", "PDF Data": pdf_summary['Gross_Total_Qty'], "Excel (Sum)": excel_df['Qty_EXCEL'].sum(), "Difference": pdf_summary['Gross_Total_Qty'] - excel_df['Qty_EXCEL'].sum(), "Status": "✅ Match" if (pdf_summary['Gross_Total_Qty'] - excel_df['Qty_EXCEL'].sum()) == 0 else "❌ Mismatch"},
                {"Metric": "Total Tax", "PDF Data": pdf_summary['Total_Tax_Footer'], "Excel (Sum)": excel_df['Tax_EXCEL'].sum(), "Difference": pdf_summary['Total_Tax_Footer'] - excel_df['Tax_EXCEL'].sum(), "Status": "✅ Match" if abs(pdf_summary['Total_Tax_Footer'] - excel_df['Tax_EXCEL'].sum()) <= tolerance else "❌ Mismatch"},
                {"Metric": "Grand Total", "PDF Data": pdf_summary['Grand_Total_Footer'], "Excel (Sum)": excel_df['Total_EXCEL'].sum(), "Difference": pdf_summary['Grand_Total_Footer'] - excel_df['Total_EXCEL'].sum(), "Status": "✅ Match" if abs(pdf_summary['Grand_Total_Footer'] - excel_df['Total_EXCEL'].sum()) <= tolerance else "❌ Mismatch"}
            ])

            # Dashboard
            st.metric("Overall Accuracy", f"{(len(comp_df[comp_df['Status'] == '✅ Match']) / len(comp_df)) * 100:.2f}%")
            st.subheader("Grand Totals Validation")
            st.table(summary_results)
            st.subheader("Item-wise Comparison")
            st.dataframe(comp_df[['Material Code', 'Total_PDF', 'Total_EXCEL', 'Status']], use_container_width=True)

            # Export
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                comp_df[['Material Code', 'Total_EXCEL', 'Total_PDF', 'Status']].to_excel(writer, sheet_name='Item_Comparison', index=False)
                summary_results.to_excel(writer, sheet_name='Grand_Totals', index=False)
            st.download_button("📥 Download Report", output.getvalue(), "reconciliation_report.xlsx")