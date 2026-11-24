import { useRef, useState } from 'react';
import { request } from './api';
import { departmentReady, type Department } from './Departments';
import { kinds } from './materialLabels';
import type { Review } from './materialTypes';
import { useResource } from './useResource';
import { useUnsavedChanges } from './useUnsavedChanges';

const acceptedFiles = '.pdf,.doc,.docx,.pptx,.xlsx,.txt,.md,image/*';
const recordings = /\.(aac|aiff?|amr|avi|flac|m4[av]|mkv|mov|mp[34]|mp[eg]|ogg|opus|wav|webm|wma|wmv)$/i;

export function Materials({ conferenceId, onPrepared }: { conferenceId: string; onPrepared: () => void }) {
  const base = `/api/v1/conferences/${conferenceId}`;
  const { data: review, error, refresh } = useResource<Review>(`${base}/materials`);
  const { data: departments, error: departmentError } = useResource<Department[]>('/api/v1/departments');
  const ready = departments?.some(departmentReady) ?? false;
  const filesInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState('');
  const allowNavigation = useUnsavedChanges(!!busy);
  const [status, setStatus] = useState('');
  const [errors, setErrors] = useState<string[]>([]);
  const files = review?.files ?? [];
  const hasTalkMaterial = files.some(file => !['program', 'audio', 'video'].includes(file.source.kind));

  async function upload(input: HTMLInputElement) {
    const selected = Array.from(input.files ?? []);
    input.value = '';
    if (!selected.length) return;
    const failures: string[] = [];
    let uploaded = 0, duplicates = 0;
    setErrors([]); setStatus('');
    const entries = selected.map(file => ({ file, path: file.webkitRelativePath ? file.webkitRelativePath.split('/').slice(1).join('/') : file.name }))
      .filter(item => !(item.file.webkitRelativePath && item.path === 'conference.yaml'))
      .sort((a, b) => Number(/\.(pptx|pdf)$/i.test(b.path)) - Number(/\.(pptx|pdf)$/i.test(a.path)) || a.path.localeCompare(b.path, 'ru'));
    try {
      for (const [index, { file, path }] of entries.entries()) {
        setBusy(`Загрузка ${index + 1} из ${entries.length}`);
        if (file.type.startsWith('audio/') || file.type.startsWith('video/') || recordings.test(path)) {
          failures.push(`${path} — Аудио и видео не поддерживаются.`);
          continue;
        }
        try {
          const response = await request(`${base}/materials?${new URLSearchParams({ path })}`, {
            method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file,
          });
          if (response.status === 200) duplicates++; else uploaded++;
        } catch (reason) { failures.push(`${path} — ${(reason as Error).message}`); }
      }
      refresh();
      setStatus(`Загружено ${uploaded}, уже было в конференции ${duplicates}.`);
    } finally { setBusy(''); setErrors(failures); }
  }

  async function prepare() {
    setBusy('Ставим анализ в очередь…'); setErrors([]);
    try {
      await request(`${base}/preparation`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ workflow: 'operator', auto_start: true }),
      });
      allowNavigation();
      onPrepared();
    }
    catch (reason) { setErrors([(reason as Error).message]); }
    finally { setBusy(''); }
  }

  return <section className="workflow-page" aria-labelledby="materials-title" aria-busy={!!busy}>
    <h2 id="materials-title">Загрузите материалы конференции</h2>
    <p className="muted">Подойдут презентации, PDF, документы, текстовые файлы и изображения.</p>
    {departmentError && <p role="alert">{departmentError}</p>}
    {departments && !ready && <p className="notice">Сначала <a href="#/departments">добавьте сведения об отделе</a> для отбора докладов.</p>}
    <div className="upload-zone">
      <div><h3>Добавьте файлы или папку</h3><p className="muted">Вложенные папки помогают связать материалы с докладами.</p></div>
      <div className="row"><button disabled={!!busy || !ready} onClick={() => filesInput.current?.click()}>Выбрать файлы</button>
        <button className="link" disabled={!!busy || !ready} onClick={() => folderInput.current?.click()}>Выбрать папку</button></div>
      <input className="sr-only" tabIndex={-1} aria-label="Отдельные файлы" type="file" accept={acceptedFiles} multiple ref={filesInput} disabled={!!busy || !ready} onChange={e => void upload(e.currentTarget)} />
      <input className="sr-only" tabIndex={-1} aria-label="Папка конференции" type="file" ref={input => { folderInput.current = input; input?.setAttribute('webkitdirectory', ''); }} disabled={!!busy || !ready} onChange={e => void upload(e.currentTarget)} />
    </div>
    <p role="status">{busy || status}</p>
    {(error || errors.length > 0) && <div role="alert">{error}<ul>{errors.map((message, index) => <li key={index}>{message}</li>)}</ul></div>}
    {!review && !error && <p className="loading">Загрузка материалов…</p>}
    {review && !files.length && <p className="empty-state">Добавьте материалы, чтобы сервис предложил темы и авторов докладов.</p>}
    {!!files.length && <div className="material-list"><div className="list-caption"><span>Название и тип</span><span>Состояние</span></div>
      {files.map(file => <details className="material-row" key={file.source.id}>
        <summary><span className="file-type">{file.relative_path.split('.').pop()?.toUpperCase()}</span>
          <span className="file-name">{file.relative_path}<small>{kinds[file.source.kind]}{file.source.duration_ms ? ` · ${Math.ceil(file.source.duration_ms / 1000)} с` : ''}</small></span>
          <span className={file.warning ? 'text-warn' : 'text-ok'}>{file.warning ? 'Нужна проверка' : 'Загружен'}</span></summary>
        <div className="material-details">
          {file.warning && <p className="notice">{file.warning}</p>}
          {['presentation', 'document', 'notes', 'program'].includes(file.source.kind) && <label className="field">Назначение файла
            <select aria-label={`Назначение ${file.relative_path}`} disabled={!!busy || !ready} value={file.source.kind} onChange={e => {
              const purpose = e.target.value;
              setBusy('Сохранение назначения…'); setErrors([]);
              void request(`/api/v1/files/${file.source.id}/purpose`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ purpose }) })
                .then(refresh).catch((reason: Error) => setErrors([reason.message])).finally(() => setBusy(''));
            }}>{['presentation', 'document', 'notes', 'program'].map(kind => <option key={kind} value={kind}>{kinds[kind as keyof typeof kinds]}</option>)}</select></label>}
          <a href={`/api/v1/files/${file.source.id}/content`} target="_blank" rel="noreferrer">Открыть оригинал</a>
          {file.title_candidates?.speakers.length ? <p>Авторы в материале — {file.title_candidates.speakers.join(', ')}</p> : null}
        </div>
      </details>)}
    </div>}
    <footer className="step-footer"><p className="muted">{hasTalkMaterial ? `Загружено файлов — ${files.length}. После анализа откроется реестр для проверки.` : 'Добавьте хотя бы один материал доклада, чтобы начать анализ.'}</p>
      <button className="primary" disabled={!!busy || !ready || !hasTalkMaterial} onClick={() => void prepare()}>Начать анализ</button></footer>
  </section>;
}
