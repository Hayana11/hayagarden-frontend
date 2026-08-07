import { useNavigate } from 'react-router-dom';
import { PageHeader } from './PageHeader';

export function BackHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  const navigate = useNavigate();
  return (
    <PageHeader
      title={title}
      subtitle={subtitle}
      onBack={() => navigate('/')}
      backLabel="返回首页"
    />
  );
}
