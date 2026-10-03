import { useMemo, useState } from 'react';
import {
  AlertCircle, CheckCircle, Download, FileText,
  ScanSearch, UploadCloud, ChevronDown, ChevronUp,
  Info,
} from 'lucide-react';
import { analyzePdf, exportSelection, getDownloadUrl } from './services/api';

// ─── Badge colours by record type ─────────────────────────────────────────
const TYPE_BADGE = {
  key_value:        { bg: '#DBEAFE', text: '#1D4ED8', label: 'Key/Value' },
  table_cell:       { bg: '#D1FAE5', text: '#065F46', label: 'Table'     },
  table_row:        { bg: '#D1FAE5', text: '#065F46', label: 'Table'     },
  heading:          { bg: '#EDE9FE', text: '#5B21B6', label: 'Heading'   },
  paragraph:        { bg: '#FEF3C7', text: '#92400E', label: 'Para'      },
  declaration:      { bg: '#FEE2E2', text: '#991B1B', label: 'Decl.'     },
  needs_review:     { bg: '#FEF2F2', text: '#B91C1C', label: 'Needs Review'},
  unstructured_text:{ bg: '#F3F4F6', text: '#374151', label: 'Text'      },
};

function TypeBadge({ type }) {
  const style = TYPE_BADGE[type] || TYPE_BADGE['unstructured_text'];
  return (
    <span
      style={{
        background: style.bg, color: style.text,
        fontSize: 10, fontWeight: 600, borderRadius: 4,
        padding: '1px 6px', whiteSpace: 'nowrap',
      }}
    >
      {style.label}
    </span>
  );
}

// ─── Confidence indicator ─────────────────────────────────────────────────
function ConfidenceDot({ confidence }) {
  if (!confidence && confidence !== 0) return null;
  const c = parseFloat(confidence);
  const color = c >= 75 ? '#10B981' : c >= 50 ? '#F59E0B' : '#EF4444';
  const title = c >= 75 ? 'High confidence' : c >= 50 ? 'Medium confidence' : 'Needs review';
  return (
    <span
      title={title}
      style={{
        display: 'inline-block', width: 8, height: 8,
        borderRadius: '50%', background: color, marginLeft: 4,
      }}
    />
  );
}

// ─── Warnings collapse ────────────────────────────────────────────────────
function WarningsPanel({ warnings }) {
  const [open, setOpen] = useState(false);
  if (!warnings || warnings.length === 0) return null;
  return (
    <div style={{ marginTop: 12, border: '1px solid #FDE68A', borderRadius: 8, overflow: 'hidden' }}>
      <button
        onClick={() => setOpen(!open)}
        style={{
          width: '100%', display: 'flex', alignItems: 'center',
          gap: 8, padding: '8px 14px', background: '#FFFBEB',
          border: 'none', cursor: 'pointer', fontWeight: 600,
          color: '#92400E', fontSize: 13,
        }}
      >
        <AlertCircle size={15} />
        {warnings.length} processing warning{warnings.length > 1 ? 's' : ''}
        {open ? <ChevronUp size={14} style={{ marginLeft: 'auto' }} /> : <ChevronDown size={14} style={{ marginLeft: 'auto' }} />}
      </button>
      {open && (
        <ul style={{ margin: 0, padding: '8px 14px 10px 30px', background: '#FFFBEB', color: '#78350F', fontSize: 12, lineHeight: 1.6 }}>
          {warnings.map((w, i) => <li key={i}>{w}</li>)}
        </ul>
      )}
    </div>
  );
}

// ─── Main App ─────────────────────────────────────────────────────────────
function App() {
  const [file,             setFile]             = useState(null);
  const [password,         setPassword]         = useState('');
  const [analysis,         setAnalysis]         = useState(null);
  const [selectedFields,   setSelectedFields]   = useState([]);
  const [result,           setResult]           = useState(null);
  const [error,            setError]            = useState('');
  const [isAnalyzing,      setIsAnalyzing]      = useState(false);
  const [isExporting,      setIsExporting]      = useState(false);
  const [passwordRequired, setPasswordRequired] = useState(false);
  const [filterType,       setFilterType]       = useState('all');

  // All detected fields (no slice, no limit)
  const allFields = analysis?.fields || [];

  // Filtered view
  const fields = useMemo(() => {
    if (filterType === 'all') return allFields;
    return allFields.filter(f => f.type === filterType);
  }, [allFields, filterType]);

  const allSelected = fields.length > 0 && fields.every(f => selectedFields.includes(f.id));

  const selectedCountLabel = useMemo(() => {
    if (!allFields.length) return 'No fields detected';
    return `${selectedFields.length} of ${allFields.length} selected`;
  }, [allFields.length, selectedFields.length]);

  // ── Type filter options ─────────────────────────────────────────────────
  const typeOptions = useMemo(() => {
    const counts = {};
    for (const f of allFields) {
      const t = f.type || 'unstructured_text';
      counts[t] = (counts[t] || 0) + 1;
    }
    return counts;
  }, [allFields]);

  // ── Reset ────────────────────────────────────────────────────────────────
  const resetForNewFile = (nextFile) => {
    setFile(nextFile);
    setPassword('');
    setPasswordRequired(false);
    setAnalysis(null);
    setSelectedFields([]);
    setResult(null);
    setError('');
    setFilterType('all');
  };

  const handleFileChange = (event) => {
    const nextFile = event.target.files?.[0];
    if (nextFile) resetForNewFile(nextFile);
  };

  // ── Analyze ──────────────────────────────────────────────────────────────
  const handleAnalyze = async () => {
    if (!file) { setError('Please select a PDF file first.'); return; }
    setError('');
    setResult(null);
    setIsAnalyzing(true);
    try {
      const data = await analyzePdf(file, 'Automatic', password);
      if (data.status === 'PASSWORD_REQUIRED') {
        setPasswordRequired(true);
        setError(password ? 'Incorrect password.' : 'This PDF is password protected.');
        return;
      }
      setPasswordRequired(false);
      setAnalysis(data);
      // Select ALL fields by default
      setSelectedFields((data.fields || []).filter(f => f.selected !== false).map(f => f.id));
    } catch (err) {
      setError(err.response?.data?.detail || 'Unable to analyze this PDF.');
    } finally {
      setIsAnalyzing(false);
    }
  };

  // ── Checkbox toggle ───────────────────────────────────────────────────────
  const toggleField = (fieldId) => {
    setSelectedFields(current =>
      current.includes(fieldId)
        ? current.filter(id => id !== fieldId)
        : [...current, fieldId]
    );
  };

  const toggleAll = () => {
    if (allSelected) {
      // Deselect all *visible* (filtered) fields
      const visibleIds = new Set(fields.map(f => f.id));
      setSelectedFields(current => current.filter(id => !visibleIds.has(id)));
    } else {
      // Select all visible fields (union with existing)
      const visibleIds = fields.map(f => f.id);
      setSelectedFields(current => Array.from(new Set([...current, ...visibleIds])));
    }
  };

  const selectAllGlobal  = () => setSelectedFields(allFields.map(f => f.id));
  const clearAllGlobal   = () => setSelectedFields([]);

  // ── Export ───────────────────────────────────────────────────────────────
  const handleExport = async (exportAll = false) => {
    if (!analysis?.conversion_id) return;
    if (!exportAll && selectedFields.length === 0) {
      setError('Please select at least one field.');
      return;
    }
    setError('');
    setIsExporting(true);
    try {
      const data = await exportSelection(analysis.conversion_id, selectedFields, exportAll);
      setResult(data);
    } catch (err) {
      setError(err.response?.data?.detail || 'Unable to create Excel file.');
    } finally {
      setIsExporting(false);
    }
  };

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 font-sans">
      {/* Header */}
      <header className="bg-white border-b border-slate-200 shadow-sm">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 py-4 sm:py-6 flex items-center gap-3">
          <div className="bg-blue-600 text-white p-2 rounded-lg shrink-0">
            <FileText size={24} />
          </div>
          <div>
            <h1 className="text-xl sm:text-2xl font-bold text-slate-800 tracking-tight">
              PDF Field Selector
            </h1>
            <p className="text-slate-500 text-xs sm:text-sm">
              Preview fields first, then export only what you need.
            </p>
          </div>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-4 sm:px-6 py-6 sm:py-10 space-y-5">

        {/* ── Upload section ── */}
        <section className="bg-white rounded-xl shadow-sm border border-slate-200 p-5 sm:p-7">
          <h2 className="text-base sm:text-lg font-semibold mb-4 flex items-center gap-2">
            <UploadCloud size={20} className="text-blue-500" /> Upload PDF
          </h2>

          <div className="border-2 border-dashed border-slate-300 rounded-lg p-8 text-center hover:bg-slate-50 transition-colors">
            <input id="file-upload" type="file" accept=".pdf" onChange={handleFileChange} className="hidden" />
            <label htmlFor="file-upload" className="cursor-pointer flex flex-col items-center gap-3">
              <FileText size={44} className="text-slate-400" />
              <span className="font-medium text-sm sm:text-base text-slate-700 px-2 text-center">
                Choose any PDF – forms, scanned, certificates, bank docs…
              </span>
              <span className="text-[10px] sm:text-xs text-slate-500 text-center">PDF will be analyzed before Excel is created</span>
            </label>
          </div>

          {file && (
            <div className="mt-4 p-3 bg-blue-50 text-blue-800 rounded-lg text-sm flex items-center gap-2 font-medium border border-blue-100">
              <CheckCircle size={18} className="shrink-0" />
              <span className="truncate">{file.name}</span>
              <span className="text-blue-600">({(file.size / 1024 / 1024).toFixed(2)} MB)</span>
            </div>
          )}

          {passwordRequired && (
            <div className="mt-4">
              <label className="block text-sm font-medium text-slate-700 mb-1">PDF Password</label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full border border-slate-300 rounded-lg p-2.5 text-sm outline-none focus:ring-2 focus:ring-blue-500"
                placeholder="Enter password and analyze again"
              />
            </div>
          )}

          <button
            onClick={handleAnalyze}
            disabled={!file || isAnalyzing}
            className={`mt-5 w-full py-3 rounded-lg font-bold text-white transition-all flex items-center justify-center gap-2 ${
              (!file || isAnalyzing) ? 'bg-slate-400 cursor-not-allowed' : 'bg-blue-600 hover:bg-blue-700 shadow-sm'
            }`}
          >
            <ScanSearch size={18} />
            {isAnalyzing ? 'Analyzing PDF…' : 'Analyze & Preview Fields'}
          </button>
        </section>

        {/* ── Error ── */}
        {error && (
          <div className="p-4 bg-red-50 text-red-700 rounded-lg text-sm flex items-start gap-2 border border-red-200">
            <AlertCircle size={18} className="shrink-0 mt-0.5" />
            <span>{error}</span>
          </div>
        )}

        {/* ── Analysis result / field preview ── */}
        {analysis && (
          <section className="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">

            {/* Header bar */}
            <div className="p-5 sm:p-6 border-b border-slate-200">
              <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                <div>
                  <h2 className="text-lg font-bold text-slate-800">Select Fields For Excel</h2>
                  <p className="text-sm text-slate-500 mt-0.5">
                    {analysis.pdf_type?.replace(/_/g, ' ')} &bull;{' '}
                    {analysis.page_count} page{analysis.page_count !== 1 ? 's' : ''} &bull;{' '}
                    {selectedCountLabel}
                    {analysis.ocr_used && (
                      <span className="ml-2 inline-flex items-center gap-1 text-xs bg-purple-100 text-purple-700 px-2 py-0.5 rounded-full font-medium">
                        <ScanSearch size={10} /> OCR used
                      </span>
                    )}
                  </p>
                </div>
                <div className="flex gap-2 flex-wrap">
                  <button
                    onClick={selectAllGlobal}
                    className="px-3 py-1.5 rounded-lg border border-slate-300 text-xs font-semibold text-slate-700 hover:bg-slate-50"
                  >
                    Select All
                  </button>
                  <button
                    onClick={clearAllGlobal}
                    className="px-3 py-1.5 rounded-lg border border-slate-300 text-xs font-semibold text-slate-700 hover:bg-slate-50"
                  >
                    Clear All
                  </button>
                  <button
                    onClick={toggleAll}
                    className="px-3 py-1.5 rounded-lg border border-blue-400 text-xs font-semibold text-blue-700 hover:bg-blue-50"
                  >
                    {allSelected ? 'Deselect Visible' : 'Select Visible'}
                  </button>
                </div>
              </div>

              {/* Type filter pills */}
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  onClick={() => setFilterType('all')}
                  className={`px-3 py-1 rounded-full text-xs font-semibold border transition-colors ${
                    filterType === 'all'
                      ? 'bg-blue-600 text-white border-blue-600'
                      : 'bg-white text-slate-600 border-slate-300 hover:bg-slate-50'
                  }`}
                >
                  All ({allFields.length})
                </button>
                {Object.entries(typeOptions).map(([type, count]) => {
                  const badge = TYPE_BADGE[type] || TYPE_BADGE['unstructured_text'];
                  return (
                    <button
                      key={type}
                      onClick={() => setFilterType(type)}
                      className={`px-3 py-1 rounded-full text-xs font-semibold border transition-colors ${
                        filterType === type
                          ? 'bg-blue-600 text-white border-blue-600'
                          : 'bg-white text-slate-600 border-slate-300 hover:bg-slate-50'
                      }`}
                    >
                      {badge.label} ({count})
                    </button>
                  );
                })}
              </div>
            </div>

            {/* Field table */}
            <div className="overflow-x-auto">
              <table className="w-full text-sm text-left">
                <thead className="bg-slate-50 text-slate-600 font-semibold border-b border-slate-200">
                  <tr>
                    <th className="px-3 sm:px-4 py-3 w-10 sm:w-12">
                      <input
                        type="checkbox"
                        checked={allSelected}
                        onChange={toggleAll}
                        className="h-5 w-5 sm:h-4 sm:w-4 accent-blue-600 cursor-pointer"
                        title="Toggle visible"
                      />
                    </th>
                    <th className="px-3 sm:px-4 py-3 min-w-[140px] sm:min-w-[180px] text-xs sm:text-sm">Field</th>
                    <th className="px-3 sm:px-4 py-3 min-w-[200px] sm:min-w-[260px] text-xs sm:text-sm">Detected Data</th>
                  </tr>
                </thead>
                <tbody>
                  {fields.length === 0 ? (
                    <tr>
                      <td colSpan="3" className="px-4 py-8 text-center text-slate-500">
                        {allFields.length > 0
                          ? 'No fields match the current filter.'
                          : 'No selectable fields detected. Try a clearer scan or upload a document with readable text.'}
                      </td>
                    </tr>
                  ) : fields.map((field) => {
                    const isSelected = selectedFields.includes(field.id);
                    return (
                      <tr
                        key={field.id}
                        onClick={() => toggleField(field.id)}
                        className={`border-b border-slate-100 cursor-pointer transition-colors ${
                          isSelected ? 'bg-blue-50/40 hover:bg-blue-50/70' : 'hover:bg-slate-50/70'
                        }`}
                      >
                        <td className="px-3 sm:px-4 py-2.5" onClick={(e) => e.stopPropagation()}>
                          <input
                            type="checkbox"
                            checked={isSelected}
                            onChange={() => toggleField(field.id)}
                            className="h-5 w-5 sm:h-4 sm:w-4 accent-blue-600 cursor-pointer"
                          />
                        </td>
                        <td className="px-3 sm:px-4 py-2.5 font-medium text-slate-800 text-xs sm:text-sm">
                          {field.field}
                          <ConfidenceDot confidence={field.confidence} />
                        </td>
                        <td className="px-3 sm:px-4 py-2.5 text-slate-600 max-w-[400px] whitespace-normal break-words text-xs sm:text-sm">
                          {field.value}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {/* Warnings */}
            {analysis.warnings?.length > 0 && (
              <div className="px-5 sm:px-6 pb-4">
                <WarningsPanel warnings={analysis.warnings} />
              </div>
            )}

            {/* Export bar */}
            <div className="p-5 sm:p-6 bg-slate-50 border-t border-slate-200 flex flex-col sm:flex-row gap-3">
              <button
                onClick={() => handleExport(false)}
                disabled={isExporting || selectedFields.length === 0}
                className={`flex-1 py-3 rounded-lg font-bold text-white flex items-center justify-center gap-2 ${
                  (isExporting || selectedFields.length === 0)
                    ? 'bg-slate-400 cursor-not-allowed'
                    : 'bg-emerald-600 hover:bg-emerald-700'
                }`}
              >
                <Download size={18} />
                {isExporting ? 'Creating Excel…' : `Export Selected (${selectedFields.length})`}
              </button>
            </div>
          </section>
        )}

        {/* ── Download result ── */}
        {result && (
          <section className="bg-emerald-50 border border-emerald-200 rounded-xl p-5 sm:p-6 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
            <div className="flex items-start gap-3">
              <CheckCircle className="text-emerald-600 mt-0.5" size={22} />
              <div>
                <h2 className="font-bold text-emerald-900">Excel is ready</h2>
                <p className="text-sm text-emerald-700">
                  Only the {selectedFields.length} selected field{selectedFields.length !== 1 ? 's' : ''} were exported.
                </p>
              </div>
            </div>
            <a
              href={getDownloadUrl(result.conversion_id)}
              download
              className="w-full sm:w-auto px-5 py-3 rounded-lg bg-emerald-600 text-white font-bold hover:bg-emerald-700 flex items-center justify-center gap-2"
            >
              <Download size={18} />
              Download Excel
            </a>
          </section>
        )}

      </main>
    </div>
  );
}

export default App;
