import { useState } from 'react'
import { Alert, Button, Empty, Input, Modal, Select, Table, Tag } from 'antd'
import { evaluationRequest, type QuestionBank, type EvaluationManifest } from './evaluationApi'
import EvaluationDataPlan from './EvaluationDataPlan'
import { DatabaseOutlined } from './Icons'

interface Props {
  banks: QuestionBank[]
  selected?: QuestionBank
  onSelect: (key: string) => void
  onImported: (bank: QuestionBank) => Promise<void>
  onUse: (bank: QuestionBank) => void
  purpose: EvaluationManifest['purpose']
  onPurpose: (purpose: EvaluationManifest['purpose']) => void
  manifest?: EvaluationManifest
  onManifest: (manifest?: EvaluationManifest) => void
}

export default function EvaluationLibrary({ banks, selected, onSelect, onImported, onUse, purpose, onPurpose, manifest, onManifest }: Props) {
  const [importing, setImporting] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [existingBank, setExistingBank] = useState<string>()
  const [file, setFile] = useState<File>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function importBank() {
    if (!name.trim() || !file) return
    setBusy(true); setError('')
    try {
      const form = new FormData()
      form.append('name', name); form.append('description', description); form.append('file', file)
      if (existingBank) form.append('bank_id', existingBank)
      const bank = await evaluationRequest<QuestionBank>('/question-banks', 'POST', form)
      await onImported(bank)
      setImporting(false); setFile(undefined)
    } catch (error) { setError(error instanceof Error ? error.message : '导入失败') }
    finally { setBusy(false) }
  }
  return <div className="evaluation-library">
    <section className="evaluation-panel">
      <div className="evaluation-section-heading"><div><h2>题库版本</h2><p>选择固定数据版本，管理题目清单与曝光记录。</p></div>
        <Button icon={<DatabaseOutlined />} onClick={() => { setError(''); setImporting(true) }}>导入题库</Button>
      </div>
      <Table<QuestionBank> size="small" rowKey={bank => `${bank.bank_id}:${bank.version}`} dataSource={banks} pagination={{ pageSize: 8 }} scroll={{ x: 580 }}
        rowSelection={{ type: 'radio', selectedRowKeys: selected ? [`${selected.bank_id}:${selected.version}`] : [], onChange: keys => onSelect(String(keys[0])) }}
        columns={[
          { title: '题库', dataIndex: 'name' }, { title: '版本', dataIndex: 'version', render: value => <Tag>v{value}</Tag> },
          { title: '题数', dataIndex: 'count', align: 'right' }, { title: '有答案', dataIndex: 'labelled', align: 'right' },
          { title: '操作', render: (_, bank) => <Button type="link" size="small" onClick={() => onUse(bank)}>用于新实验</Button> },
        ]} />
    </section>
    <section className="evaluation-panel">
      <div className="evaluation-section-heading"><div><h2>固定清单与数据管理</h2><p>{selected ? `${selected.name} · v${selected.version} · ${selected.count} 题` : '先选择一个题库版本'}</p></div></div>
      {selected ? <EvaluationDataPlan key={`${selected.bank_id}:${selected.version}`} bank={selected} purpose={purpose} onPurpose={onPurpose} manifest={manifest} onManifest={onManifest} />
        : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选择题库后管理固定清单" />}
    </section>
    <Modal className="evaluation-console-modal" title="导入问题库" open={importing} onCancel={() => !busy && setImporting(false)} destroyOnHidden
      footer={<div className="evaluation-button-row"><Button disabled={busy} onClick={() => setImporting(false)}>取消</Button>
        <Button type="primary" loading={busy} disabled={!name.trim() || !file} onClick={() => void importBank()}>导入并保存固定版本</Button></div>}>
      {error && <Alert type="error" showIcon title={error} />}
      <div className="evaluation-form-grid">
        <label className="evaluation-span-full">问题库名称<Input aria-label="问题库名称" value={name} maxLength={100} onChange={event => setName(event.target.value)} /></label>
        <label className="evaluation-span-full">说明<Input.TextArea aria-label="问题库说明" value={description} maxLength={2000} onChange={event => setDescription(event.target.value)} /></label>
        <label className="evaluation-span-full">保存位置<Select aria-label="问题库保存位置" allowClear placeholder="创建新问题库" value={existingBank}
          options={[...new Map(banks.map(bank => [bank.bank_id, { value: bank.bank_id, label: bank.name }])).values()]} onChange={setExistingBank} /></label>
        <label className="evaluation-file-picker evaluation-span-full">问题 JSON 文件<input aria-label="问题 JSON 文件" type="file" accept=".json,application/json" onChange={event => setFile(event.target.files?.[0])} /></label>
      </div>
      <p className="evaluation-hint">支持 HotpotQA JSON 数组，或包含 id、question、answer 的问题数组。有标准答案才计算 EM/F1，每次导入生成固定版本。</p>
    </Modal>
  </div>
}
