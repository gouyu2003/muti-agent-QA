import { useState } from 'react'
import { Button, Dropdown, Modal } from 'antd'
import { MoreHorizontal } from 'lucide-react'

type Props = {
  name: string
  archived?: boolean
  active: boolean
  busy: boolean
  onArchive: () => void
  onDelete: () => Promise<unknown>
}

export default function EvaluationJobActions({ name, archived, active, busy, onArchive, onDelete }: Props) {
  const [confirmDelete, setConfirmDelete] = useState(false)

  return <>
    <Dropdown trigger={['click']} menu={{ items: [
      { key: 'archive', label: archived ? '取消归档' : '归档实验', disabled: active || busy, onClick: onArchive },
      { key: 'delete', label: '删除实验', danger: true, disabled: active || busy, onClick: () => setConfirmDelete(true) },
    ] }}>
      <Button className="evaluation-card-actions" type="text" disabled={busy}
        title={active ? '请等实验暂停或结束后再操作' : '更多实验操作'}
        aria-label="更多实验操作" aria-description={name} icon={<MoreHorizontal size={17} aria-hidden="true" />} />
    </Dropdown>
    {confirmDelete && <Modal open title="永久删除实验？" okText="删除实验" cancelText="取消"
      okButtonProps={{ danger: true, disabled: active }} confirmLoading={busy}
      onCancel={() => { if (!busy) setConfirmDelete(false) }}
      onOk={async () => {
        await onDelete()
        setConfirmDelete(false)
      }}>
      <p>删除「{name}」后，该实验的结果和逐轮记录将无法恢复。</p>
    </Modal>}
  </>
}
