type Job = 'queued' | 'running' | 'done' | 'failed';
export type TalkProgress = {
  id: string;
  section: string;
  order: number;
  title: string | null;
  status: string;
  job: Job | null;
  step: 'parse' | 'facts' | 'verify' | 'summary' | null;
  error: string | null;
  summary_number: number | null;
  bulk_reviewable?: boolean;
  summary_status: 'ready' | 'needs_attention' | 'stale' | null;
  /** Выбор оператора: по интересу отделов, сводка или только карточка. */
  scope?: Scope;
  /** Итог обработки: докладу достаточно карточки, сводки нет. */
  card_only?: boolean;
};
export type Scope = 'auto' | 'summary' | 'card';
export type Progress = {
  processing_started: boolean;
  files: number;
  talks: TalkProgress[];
};

