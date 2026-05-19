import { useMemo, useState } from 'react';
import axios from 'axios';

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:5000';

export default function App() {
  const [file, setFile] = useState(null);
  const [sessionId, setSessionId] = useState('');
  const [resumePreview, setResumePreview] = useState('');
  const [jobDescription, setJobDescription] = useState('');
  const [analysis, setAnalysis] = useState(null);
  const [suggestedAdditions, setSuggestedAdditions] = useState([]);
  const [approvedAdditions, setApprovedAdditions] = useState([]);
  const [rewrittenText, setRewrittenText] = useState('');
  const [downloadPdf, setDownloadPdf] = useState('');
  const [previewPdf, setPreviewPdf] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [generated, setGenerated] = useState(false);

  const selectedAdditionSet = useMemo(() => new Set(approvedAdditions), [approvedAdditions]);

  const uploadResume = async () => {
    setError('');
    setGenerated(false);
    setAnalysis(null);
    setRewrittenText('');
    setDownloadPdf('');
    setPreviewPdf('');
    if (!file) {
      setError('Please choose a PDF resume.');
      return;
    }
    const form = new FormData();
    form.append('file', file);
    setBusy(true);
    try {
      const res = await axios.post(`${API_BASE}/upload`, form);
      setSessionId(res.data.session_id);
      setResumePreview(res.data.resume_text_preview || '');
      setSuggestedAdditions(res.data.detected_keywords || []);
      setApprovedAdditions([]);
    } catch (err) {
      const message = err?.response?.data?.details
        ? `${err?.response?.data?.error || 'Upload failed'}: ${err.response.data.details}`
        : err?.response?.data?.error || err?.message || 'Upload failed';
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  const analyzeResume = async () => {
    setError('');
    setGenerated(false);
    if (!sessionId) {
      setError('Upload a resume first.');
      return;
    }
    if (!jobDescription.trim()) {
      setError('Paste a job description.');
      return;
    }
    setBusy(true);
    try {
      const res = await axios.post(`${API_BASE}/analyze`, {
        session_id: sessionId,
        job_description: jobDescription,
      });
      setAnalysis(res.data);
      setSuggestedAdditions(res.data.suggested_additions || []);
      setApprovedAdditions([]);
    } catch (err) {
      setError(err?.response?.data?.error || 'Analyze failed');
    } finally {
      setBusy(false);
    }
  };

  const rewriteResume = async () => {
    setError('');
    if (!sessionId) {
      setError('Upload a resume first.');
      return;
    }
    setBusy(true);
    try {
      const res = await axios.post(`${API_BASE}/rewrite`, {
        session_id: sessionId,
        job_description: jobDescription,
        approved_additions: approvedAdditions.filter(Boolean),
        remove_terms: [],
        confirm_removals: false,
        output_format: 'both',
      });
      setRewrittenText(res.data.rewritten_text || '');
      setDownloadPdf(`${API_BASE}${res.data.download_pdf}`);
      setPreviewPdf(`${API_BASE}${res.data.preview_pdf}`);
      setGenerated(true);
    } catch (err) {
      setError(err?.response?.data?.error || 'Rewrite failed');
    } finally {
      setBusy(false);
    }
  };

  const toggleAddition = (item) => {
    setApprovedAdditions((current) =>
      current.includes(item)
        ? current.filter((x) => x !== item)
        : [...current, item]
    );
  };

  const openPdf = () => {
    if (!previewPdf) {
      setError('Generate the resume first.');
      return;
    }
    window.open(previewPdf, '_blank', 'noopener,noreferrer');
  };

  return (
    <div className="page">
      <div className="shell">
        <header className="hero">
          <div>
            <p className="eyebrow">Resume Optimizer AI</p>
            <h1>Upload resume, paste job description, generate a better version.</h1>
            <p className="sub">
              Honest resume tailoring with permission-based additions, match scoring,
              and downloadable DOCX/PDF output.
            </p>
          </div>
          <a className="ghost" href={`${API_BASE}/health`} target="_blank" rel="noreferrer">
            Backend Health
          </a>
        </header>

        <section className="grid">
          <div className="panel">
            <h2>1. Upload Resume</h2>
            <input type="file" accept=".pdf" onChange={(e) => setFile(e.target.files?.[0] || null)} />
            <button onClick={uploadResume} disabled={busy}>Upload</button>
            {sessionId && <p className="meta">Session: {sessionId}</p>}
            {resumePreview && <pre className="preview">{resumePreview}</pre>}
          </div>

          <div className="panel">
            <h2>2. Paste Job Description</h2>
            <textarea
              rows="12"
              placeholder="Paste the job description here"
              value={jobDescription}
              onChange={(e) => setJobDescription(e.target.value)}
            />
            <div className="row">
              <button onClick={analyzeResume} disabled={busy}>Analyze</button>
              <button className="secondary" onClick={rewriteResume} disabled={busy}>Rewrite</button>
            </div>
            {error && <p className="error">{error}</p>}
          </div>
        </section>

        {analysis && (
          <section className="panel">
            <h2>3. Analysis</h2>
            <div className="score">{analysis.match_score}% Match</div>
            <div className="two-col">
              <div>
                <h3>Matched</h3>
                <ul>
                  {analysis.matched_keywords?.map((item) => <li key={item}>{item}</li>)}
                </ul>
              </div>
              <div>
                <h3>Missing</h3>
                <ul>
                  {analysis.missing_keywords?.map((item) => <li key={item}>{item}</li>)}
                </ul>
              </div>
            </div>
          </section>
        )}

        {suggestedAdditions.length > 0 && (
          <section className="panel">
            <h2>Permission for Extra Keywords</h2>
            <p>Select only the items you want the resume to include. Leave everything unselected if you want the original resume only.</p>
            <div className="chips">
              {suggestedAdditions.map((item) => (
                <button
                  key={item}
                  className={selectedAdditionSet.has(item) ? 'chip active' : 'chip'}
                  onClick={() => toggleAddition(item)}
                >
                  {item}
                </button>
              ))}
            </div>
            <p className="meta">Nothing will be removed from your upload. Only the selected keywords will be appended.</p>
            <div className="row">
              <button onClick={rewriteResume} disabled={busy || !sessionId}>
                Generate Resume PDF
              </button>
            </div>
          </section>
        )}

        {rewrittenText && (
          <section className="panel">
            <h2>4. Rewritten Resume Preview</h2>
            {generated && <p className="meta">Resume generated successfully. Download below.</p>}
            <pre className="preview large">{rewrittenText}</pre>
            <div className="row">
              <a className="download alt" href={downloadPdf}>Download PDF</a>
              <button className="secondary" onClick={openPdf} disabled={!previewPdf}>
                Open PDF
              </button>
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
