import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Users, Map, BookOpen, ScrollText, Eye, Ear, Library, MessageCircle, Swords, EyeOff, Heart, Star } from 'lucide-react'
import { useState } from 'react'

export default function RoomPage() {
  const navigate = useNavigate()
  const [input, setInput] = useState('')
  const [showSheet, setShowSheet] = useState(false)
  const [showSkills, setShowSkills] = useState(false)
  const [showMap, setShowMap] = useState(false)
  const [showNotes, setShowNotes] = useState(false)

  const messages = [
    { type: 'system', content: '案件档案已加载 · 惠特利旧宅', time: '19:03' },
    { type: 'narr', sender: '守秘人', content: '你们站在惠特利旧宅的铁门前。门虚掩着，里面传来一股潮湿的木头和尘土气息。锁孔旁有新鲜的划痕——有人比你们先到了。', time: '19:03' },
    { type: 'narr', sender: '守秘人', content: '前院的喷泉早已干涸，藤蔓爬满了石像。二楼的窗户里透出微弱的蓝绿色光芒。', time: '19:04' },
    { type: 'player', sender: '杰克·布朗', content: '我检查一下门锁，看看周围有没有脚印。', time: '19:05' },
  ]

  const skills = [
    { name: '侦察', value: 65, icon: Eye },
    { name: '聆听', value: 50, icon: Ear },
    { name: '图书馆', value: 60, icon: Library },
    { name: '说服', value: 45, icon: MessageCircle },
    { name: '斗殴', value: 35, icon: Swords },
    { name: '潜行', value: 40, icon: EyeOff },
    { name: '心理学', value: 30, icon: Heart },
    { name: '急救', value: 40, icon: Heart },
    { name: '神秘学', value: 25, icon: Star },
  ]

  return (
    <div className="h-screen flex flex-col bg-card relative">
      {/* Header */}
      <div className="flex items-center gap-2.5 px-4 py-2.5 border-b border-border-light bg-page flex-shrink-0">
        <button onClick={() => navigate('/lobby')} className="w-8 h-8 rounded-full bg-card border border-border-light flex items-center justify-center active:bg-panel">
          <ArrowLeft className="w-4 h-4 text-text-muted" strokeWidth={2.5} />
        </button>
        <div className="w-8 h-8 rounded-full bg-[#f3eef8] flex items-center justify-center text-base flex-shrink-0">
          🏚️
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-sm font-semibold text-text-primary">惠特利旧宅</div>
          <div className="text-[11px] text-text-muted">3 位调查员 · 克苏鲁的呼唤</div>
        </div>
        <div className="flex gap-1.5">
          <button className="w-8 h-8 rounded-full bg-card border border-border-light flex items-center justify-center active:bg-panel">
            <Users className="w-4 h-4 text-text-muted" strokeWidth={2.5} />
          </button>
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-4 py-4 flex flex-col gap-3">
        {messages.map((msg, i) => {
          if (msg.type === 'system') {
            return (
              <div key={i} className="text-center py-1.5">
                <span className="text-[11px] text-text-dim bg-panel px-3.5 py-1 rounded-[99px] font-mono">{msg.content}</span>
              </div>
            )
          }

          const isPlayer = msg.type === 'player'
          const isNarr = msg.type === 'narr'

          return (
            <div key={i} className={`flex gap-2.5 ${isPlayer ? 'flex-row-reverse' : ''} animate-[msgIn_0.3s_ease]`}>
              <div className={`w-8 h-8 rounded-full flex-shrink-0 flex items-center justify-center text-sm border border-border-light ${isNarr ? 'bg-[#faf5eb] border-brass' : isPlayer ? 'bg-[#eef6ee]' : 'bg-panel'}`}>
                {isNarr ? '📜' : isPlayer ? '🔍' : '🤖'}
              </div>
              <div className={`flex-1 min-w-0 ${isPlayer ? 'text-right' : ''}`}>
                <div className={`text-[11px] font-semibold text-text-muted mb-0.5 ${isPlayer ? 'text-mold' : ''} ${isNarr ? 'text-brass-dark' : ''}`}>
                  {msg.sender}
                </div>
                <div className={`
                  text-sm leading-[1.65] text-text-body inline-block max-w-full px-3.5 py-2.5
                  ${isPlayer ? 'bg-[#eef6ee] rounded-md' : ''}
                  ${isNarr ? 'bg-[#fdfaf4] border-l-[3px] border-brass rounded-r-sm rounded-l-none italic text-[#4a4030]' : ''}
                  ${!isPlayer && !isNarr ? 'bg-panel rounded-md' : ''}
                `}>
                  {msg.content}
                </div>
                <div className="text-[10px] text-text-dim mt-0.5">{msg.time}</div>
              </div>
            </div>
          )
        })}
      </div>

      {/* Action Bar */}
      <div className="flex bg-card border-t border-border-light flex-shrink-0">
        {[
          { icon: ScrollText, label: '角色卡', action: () => setShowSheet(true) },
          { icon: Star, label: '技能', action: () => setShowSkills(true) },
          { icon: Map, label: '地图', action: () => setShowMap(true) },
          { icon: BookOpen, label: '速记', action: () => setShowNotes(true) },
        ].map((item) => (
          <button
            key={item.label}
            onClick={item.action}
            className="flex-1 py-1.5 px-1 bg-none border-none text-text-muted text-[10px] font-medium cursor-pointer flex flex-col items-center gap-[3px] font-sans active:text-brass-dark active:bg-panel"
          >
            <item.icon className="w-5 h-5" strokeWidth={1.5} />
            {item.label}
          </button>
        ))}
      </div>

      {/* Input */}
      <div className="border-t border-border-light px-3 pb-3 pt-1.5 bg-page flex-shrink-0">
        <div className="flex gap-1.5 overflow-x-auto pb-1.5 -webkit-overflow-scrolling:touch">
          {['🎲 掷骰', '🔍 调查', '💬 对话', '🚶 移动'].map((q) => (
            <button key={q} className="px-3 py-1 text-[11px] bg-card border border-border-light rounded-[20px] text-text-muted whitespace-nowrap flex-shrink-0 font-sans active:bg-panel active:border-brass active:text-brass-dark">{q}</button>
          ))}
        </div>
        <div className="flex gap-2 items-end">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="输入行动…"
            className="flex-1 bg-input border border-border-mid rounded-[20px] px-4 py-2.5 text-sm text-text-primary font-sans outline-none min-h-[40px] placeholder:text-text-dim"
          />
          <button className="w-10 h-10 rounded-full bg-brass border-none text-white flex items-center justify-center flex-shrink-0 active:scale-[0.92] transition-all">
            <svg className="w-[18px] h-[18px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.5} strokeLinecap="round" strokeLinejoin="round"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>
          </button>
        </div>
      </div>
    </div>
  )
}
