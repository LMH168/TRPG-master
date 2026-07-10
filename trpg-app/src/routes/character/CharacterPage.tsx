import { useNavigate } from 'react-router-dom'
import { useState } from 'react'
import { ArrowLeft, Plus, Minus } from 'lucide-react'

const OCCUPATIONS = [
  { id: 'detective', name: '私家侦探', icon: '🔍' },
  { id: 'journalist', name: '记者', icon: '📰' },
  { id: 'professor', name: '教授', icon: '🎓' },
  { id: 'doctor', name: '医生', icon: '🏥' },
  { id: 'archaeologist', name: '考古学家', icon: '🏛️' },
  { id: 'antique', name: '古董商', icon: '🔮' },
  { id: 'writer', name: '作家', icon: '✍️' },
  { id: 'other', name: '其他', icon: '📌' },
]

const INITIAL_STATS: Record<string, number> = { str: 50, con: 50, pow: 50, dex: 50, app: 50, siz: 50, int: 50, edu: 50 }

const STAT_LABELS: Record<string, string> = {
  str: 'STR', con: 'CON', pow: 'POW', dex: 'DEX',
  app: 'APP', siz: 'SIZ', int: 'INT', edu: 'EDU',
}

const STAT_NAMES: Record<string, string> = {
  str: '力量', con: '体质', pow: '意志', dex: '敏捷',
  app: '外貌', siz: '体型', int: '智力', edu: '教育',
}

export default function CharacterPage() {
  const navigate = useNavigate()
  const [step, setStep] = useState(0)
  const [selectedOcc, setSelectedOcc] = useState('')
  const [stats, setStats] = useState({ ...INITIAL_STATS })
  const [form, setForm] = useState({
    name: '', playerName: '', age: '28', gender: '男',
    residence: '阿卡姆', birthplace: '阿卡姆',
  })

  const updateStat = (key: string, delta: number) => {
    setStats(prev => ({
      ...prev,
      [key]: Math.max(15, Math.min(99, (prev[key] || 0) + delta))
    }))
  }

  const steps = [
    { label: '基础信息', done: step > 0 },
    { label: '属性', done: step > 1 },
    { label: '技能', done: step > 2 },
    { label: '完成', done: step > 3 },
  ]

  const handleNext = () => {
    if (step < 3) setStep(s => s + 1)
    else navigate('/lobby')
  }

  const handleBack = () => {
    if (step > 0) setStep(s => s - 1)
    else navigate(-1)
  }

  return (
    <div className="animate-screen-in">
      {/* Header */}
      <div className="flex items-center gap-2.5 px-5 pb-3 pt-1">
        <button onClick={handleBack} className="w-[34px] h-[34px] rounded-full bg-card border border-border-light flex items-center justify-center flex-shrink-0 active:bg-panel active:scale-[0.94] transition-all duration-150">
          <ArrowLeft className="w-[18px] h-[18px] text-text-muted" strokeWidth={2.5} />
        </button>
        <h2 className="text-lg font-bold text-text-primary">创建角色</h2>
      </div>

      {/* Progress bar */}
      <div className="flex gap-1.5 px-5 pb-4">
        {steps.map((s, i) => (
          <div
            key={i}
            className={`flex-1 h-[3px] rounded-[99px] transition-all duration-300 ${s.done ? 'bg-brass-dark' : i === step ? 'bg-brass' : 'bg-border-light'}`}
          />
        ))}
      </div>

      {/* Step 0: Basic Info + Occupation */}
      {step === 0 && (
        <div className="px-5 pb-4 animate-screen-in">
          <div className="bg-card border border-border-light rounded-md p-[18px] mb-3">
            <h4 className="text-[12px] font-semibold text-brass-dark uppercase tracking-[0.08em] mb-[14px]">基础信息</h4>
            <div className="space-y-[14px]">
              <div>
                <label className="block text-[12px] font-medium text-text-muted mb-[5px]">角色姓名</label>
                <input
                  value={form.name}
                  onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                  className="w-full px-[13px] py-[11px] rounded-[6px] bg-input border border-border-light text-text-primary text-[15px] font-sans outline-none focus:border-brass focus:shadow-[0_0_0_3px_rgba(184,151,106,0.1)]"
                  placeholder="输入角色姓名"
                />
              </div>
              <div>
                <label className="block text-[12px] font-medium text-text-muted mb-[5px]">玩家昵称</label>
                <input
                  value={form.playerName}
                  onChange={e => setForm(f => ({ ...f, playerName: e.target.value }))}
                  className="w-full px-[13px] py-[11px] rounded-[6px] bg-input border border-border-light text-text-primary text-[15px] font-sans outline-none focus:border-brass"
                  placeholder="输入你的昵称"
                />
              </div>
              <div className="grid grid-cols-2 gap-2.5">
                <div>
                  <label className="block text-[12px] font-medium text-text-muted mb-[5px]">年龄</label>
                  <input
                    value={form.age}
                    onChange={e => setForm(f => ({ ...f, age: e.target.value }))}
                    className="w-full px-[13px] py-[11px] rounded-[6px] bg-input border border-border-light text-text-primary text-[15px] font-sans outline-none focus:border-brass"
                  />
                </div>
                <div>
                  <label className="block text-[12px] font-medium text-text-muted mb-[5px]">性别</label>
                  <select
                    value={form.gender}
                    onChange={e => setForm(f => ({ ...f, gender: e.target.value }))}
                    className="w-full px-[13px] py-[11px] rounded-[6px] bg-input border border-border-light text-text-primary text-[15px] font-sans outline-none focus:border-brass"
                  >
                    <option>男</option>
                    <option>女</option>
                    <option>其他</option>
                  </select>
                </div>
              </div>
              <div>
                <label className="block text-[12px] font-medium text-text-muted mb-[5px]">居住地</label>
                <input
                  value={form.residence}
                  onChange={e => setForm(f => ({ ...f, residence: e.target.value }))}
                  className="w-full px-[13px] py-[11px] rounded-[6px] bg-input border border-border-light text-text-primary text-[15px] font-sans outline-none focus:border-brass"
                />
              </div>
              <div>
                <label className="block text-[12px] font-medium text-text-muted mb-[5px]">出生地</label>
                <input
                  value={form.birthplace}
                  onChange={e => setForm(f => ({ ...f, birthplace: e.target.value }))}
                  className="w-full px-[13px] py-[11px] rounded-[6px] bg-input border border-border-light text-text-primary text-[15px] font-sans outline-none focus:border-brass"
                />
              </div>
            </div>
          </div>

          <div className="bg-card border border-border-light rounded-md p-[18px]">
            <h4 className="text-[12px] font-semibold text-brass-dark uppercase tracking-[0.08em] mb-[14px]">选择职业</h4>
            <div className="grid grid-cols-2 gap-2">
              {OCCUPATIONS.map(occ => (
                <div
                  key={occ.id}
                  onClick={() => setSelectedOcc(occ.id)}
                  className={`px-[10px] py-[14px] bg-input border rounded-[6px] text-center cursor-pointer active:scale-[0.96] transition-all duration-150 ${
                    selectedOcc === occ.id
                      ? 'border-brass bg-[#fdfaf4] shadow-[0_0_0_2px_rgba(184,151,106,0.15)]'
                      : 'border-border-light'
                  }`}
                >
                  <div className="text-[22px] mb-[4px]">{occ.icon}</div>
                  <div className="text-[12px] font-semibold text-text-primary">{occ.name}</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Step 1: Stats */}
      {step === 1 && (
        <div className="px-5 pb-4 animate-screen-in">
          <div className="bg-card border border-border-light rounded-md p-[18px]">
            <h4 className="text-[12px] font-semibold text-brass-dark uppercase tracking-[0.08em] mb-[14px]">属性分配</h4>
            <p className="text-[11px] text-text-muted mb-3">点击 +/- 调整属性值（范围 15-99）</p>
            <div className="grid grid-cols-2 gap-2">
              {Object.keys(INITIAL_STATS).map(key => (
                <div key={key} className="flex items-center gap-2 px-[10px] py-2 bg-input border border-border-light rounded-[6px]">
                  <div className="text-[11px] font-bold text-text-muted min-w-[30px] font-mono">{STAT_LABELS[key]}</div>
                  <div className="text-[11px] text-text-dim flex-1">{STAT_NAMES[key]}</div>
                  <button
                    onClick={() => updateStat(key, -5)}
                    className="w-[26px] h-[26px] rounded-full bg-card border border-border-light text-text-muted flex items-center justify-center active:bg-panel active:scale-[0.9]"
                  >
                    <Minus className="w-3 h-3" />
                  </button>
                  <div className="text-[15px] font-bold text-text-primary min-w-[26px] text-center">{stats[key]}</div>
                  <button
                    onClick={() => updateStat(key, 5)}
                    className="w-[26px] h-[26px] rounded-full bg-card border border-border-light text-text-muted flex items-center justify-center active:bg-panel active:scale-[0.9]"
                  >
                    <Plus className="w-3 h-3" />
                  </button>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Step 2: Skills (placeholder) */}
      {step === 2 && (
        <div className="px-5 pb-4 animate-screen-in">
          <div className="bg-card border border-border-light rounded-md p-[18px] text-center">
            <div className="w-12 h-12 rounded-[14px] bg-[#f3eef8] flex items-center justify-center mx-auto mb-3">
              <span className="text-xl">🎯</span>
            </div>
            <h4 className="text-[14px] font-semibold text-text-primary mb-1">技能分配</h4>
            <p className="text-[12px] text-text-muted leading-[1.6]">选择一个职业后将自动分配技能点<br />后续版本可自由调整</p>
            <div className="flex flex-wrap gap-2 justify-center mt-4">
              {['侦察', '聆听', '图书馆', '说服', '潜行', '心理学', '急救', '神秘学'].map(s => (
                <span key={s} className="text-[11px] bg-panel border border-border-light rounded-[99px] px-3 py-1 text-text-muted">{s}</span>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Step 3: Complete (placeholder) */}
      {step === 3 && (
        <div className="px-5 pb-4 animate-screen-in">
          <div className="bg-card border border-border-light rounded-md p-[18px] text-center">
            <div className="w-12 h-12 rounded-[14px] bg-[#eef6ee] flex items-center justify-center mx-auto mb-3">
              <span className="text-xl">📋</span>
            </div>
            <h4 className="text-[14px] font-semibold text-text-primary mb-1">装备与背景</h4>
            <p className="text-[12px] text-text-muted leading-[1.6]">设定角色的装备、背景故事和其他细节</p>
            <div className="mt-4 space-y-2">
              <div className="text-left">
                <label className="block text-[12px] font-medium text-text-muted mb-[5px]">个人物品</label>
                <input className="w-full px-[13px] py-[11px] rounded-[6px] bg-input border border-border-light text-text-primary text-[15px] font-sans outline-none" placeholder="手电筒、笔记本、相机…" />
              </div>
              <div className="text-left">
                <label className="block text-[12px] font-medium text-text-muted mb-[5px]">背景故事</label>
                <textarea className="w-full px-[13px] py-[11px] rounded-[6px] bg-input border border-border-light text-text-primary text-[15px] font-sans outline-none resize-none h-[80px]" placeholder="简单描述你的角色背景…" />
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Action buttons */}
      <div className="flex gap-2.5 px-5 pt-2 pb-4">
        <button
          onClick={step === 0 ? () => navigate(-1) : () => setStep(s => s - 1)}
          className="flex-1 flex items-center justify-center gap-2 px-6 py-3.5 rounded-sm text-sm font-semibold cursor-pointer transition-all duration-150 border-none font-sans active:scale-[0.97] bg-card text-text-body border border-border-mid active:bg-panel"
        >
          上一步
        </button>
        <button
          onClick={handleNext}
          className="flex-1 flex items-center justify-center gap-2 px-6 py-3.5 rounded-sm text-sm font-semibold cursor-pointer transition-all duration-150 border-none font-sans active:scale-[0.97] bg-brass text-white active:bg-brass-dark"
        >
          {step === 3 ? '完成创建' : '下一步'} →
        </button>
      </div>
    </div>
  )
}
