import { useCallback, useEffect, useState } from 'react';
import { fetchTodos, toggleTodo as apiToggleTodo } from '../lib/api';
import type { Todo } from '../types';

export function useTodos() {
  const [todos, setTodos] = useState<Todo[]>([]);

  useEffect(() => {
    fetchTodos().then(setTodos);
  }, []);

  const toggle = useCallback((id: Todo['id']) => {
    setTodos((prev) => {
      const target = prev.find((t) => t.id === id);
      if (target) apiToggleTodo(id, !target.done);
      return prev.map((t) => (t.id === id ? { ...t, done: !t.done } : t));
    });
  }, []);

  return { todos, toggle };
}
