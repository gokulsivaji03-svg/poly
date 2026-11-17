import { Link } from 'react-router'
import { Panel } from '@/components/common/panel'
import { EmptyState } from '@/components/common/states'
import { Button } from '@/components/ui/button'

export function NotFoundPage() {
  return (
    <Panel>
      <EmptyState
        title="Page not found"
        description="This route does not exist."
        action={
          <Button asChild size="xs" variant="outline">
            <Link to="/">Back to dashboard</Link>
          </Button>
        }
      />
    </Panel>
  )
}
