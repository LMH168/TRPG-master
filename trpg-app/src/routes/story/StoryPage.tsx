import { useNavigate } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'

export default function StoryPage() {
  const navigate = useNavigate()

  return (
    <div className="min-h-screen bg-gradient-to-b from-[#1a1620] to-[#0d0b10] flex flex-col justify-center px-7 py-10 relative">
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_50%_30%,rgba(112,80,160,0.08),transparent_70%)] pointer-events-none" />
      <button
        onClick={() => navigate(-1)}
        className="absolute top-4 left-4 w-[34px] h-[34px] rounded-full bg-[rgba(255,255,255,0.08)] border border-[rgba(255,255,255,0.12)] flex items-center justify-center text-[#a09888] z-10"
      >
        <ArrowLeft className="w-[18px] h-[18px]" />
      </button>

      <div className="font-mono text-[11px] tracking-[0.15em] text-[#706090] mb-5">
        案件档案 #1927-03
      </div>
      <h1 className="text-[28px] font-bold text-[#eeead8] leading-[1.25] mb-2">
        惠特利旧宅
      </h1>
      <p className="font-mono text-xs text-[#9088a0] mb-8 tracking-[0.05em]">
        THE WHATELEY ESTATE
      </p>
      <div className="w-10 h-px bg-[#504860] mb-7" />
      <div className="text-sm leading-[1.9] text-[#c8c0b8]">
        <p className="mb-4">
          阿卡姆，1927 年 3 月。一封匿名信将你们召集到这座废弃已久的宅邸前。
        </p>
        <p className="mb-4">
          铁门上挂着一把崭新的挂锁，但锁孔旁有新鲜的划痕——有人比你们先到了。
        </p>
        <p>
          夕阳西下，宅邸的窗户像空洞的眼窝注视着你们。
          <span className="text-[#b0a0d0] italic"> 风里带着一股若有若无的霉味。</span>
        </p>
      </div>
      <button
        onClick={() => navigate('/character')}
        className="mt-10 self-start px-6 py-3.5 rounded-sm bg-brass text-white text-sm font-semibold active:bg-brass-dark transition-all"
      >
        继续 →
      </button>
    </div>
  )
}
