export type ConferenceItem = {
  id: string;
  title: string | null;
  start_date: string | null;
  end_date: string | null;
  place: string | null;
  organizer: string | null;
  processing_started: boolean;
  talks: number;
  summaries: number;
  reviewed: number;
};

