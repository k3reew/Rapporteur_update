/** Решение модели, которое оператор ещё не подтвердил: обработка ждёт (interest.py). */
export function awaitsOperator(value: Screening | undefined) {
  return !!value && !value.manual && ['relevant', 'not_relevant', 'uncertain'].includes(value.state);
}

export type Screening = {
  version: number;
  state: string;
  eligible: boolean;
  stale: boolean;
  manual: boolean;
  error: string | null;
  department_ids: string[];
  /** interest — уверенность модели, что доклад интересен отделу, %; null — решение без модели. */
  items: { department_id: string; department_name: string; status: string; interest?: number | null; reason: string; evidence: { label: string; quote: string }[]; paragraphs: { label: string; quote: string }[] }[];
};
/** paused — приостановлено после перезапуска приложения до команды оператора. */
type Job = 'queued' | 'running' | 'done' | 'failed' | 'paused';
export type TalkProgress = {
  id: string;
  section: string;
  order: number;
  title: string | null;
  status: string;
  job: Job | null;
  step: 'screening' | 'parse' | 'facts' | 'verify' | 'summary' | null;
  error: string | null;
  screening?: Screening;
  summary_number: number | null;
  bulk_reviewable?: boolean;
  summary_status: 'ready' | 'needs_attention' | 'stale' | null;
  /** Итог обработки: доклад без интереса отделов, достаточно карточки, сводки нет. */
  card_only?: boolean;
  /** Обработка соответствует актуальному отбору: доклад можно выбрать для справки. */
  reportable?: boolean;
};
export type Progress = {
  processing_started: boolean;
  files: number;
  talks: TalkProgress[];
};

