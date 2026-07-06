import { useEffect, useState } from 'react';
import { fetchBookCurrent } from '../lib/api';
import type { BookCurrent } from '../types';

export function useBook(): BookCurrent | null {
  const [book, setBook] = useState<BookCurrent | null>(null);
  useEffect(() => {
    fetchBookCurrent().then(setBook);
  }, []);
  return book;
}
