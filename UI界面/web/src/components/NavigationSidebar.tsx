import React from 'react';
import {
  LayoutDashboard,
  Database,
  Sliders,
  History,
  Activity,
  Layers,
} from 'lucide-react';

interface NavigationSidebarProps {
  currentNav: string;
  onSelectNav: (nav: string) => void;
  /** 实时行情采集链路：最新价格事件距今秒数（来自 /health），未知为 null */
  realtimeAgeSeconds: number | null;
  /** 实时行情采集链路状态（ok / stale / ...），未知为 null */
  realtimeStatus: string | null;
}

/** 新鲜度阈值：与数据层一致，超过 60s 不再显示“在线”态。 */
const FRESH_SECONDS = 60;

export const NavigationSidebar: React.FC<NavigationSidebarProps> = ({
  currentNav,
  onSelectNav,
  realtimeAgeSeconds,
  realtimeStatus,
}) => {
  const navItems = [
    { id: 'workbench', label: '交易工作台', icon: LayoutDashboard, badge: 'Live' },
    { id: 'datacenter', label: '数据中心', icon: Database },
    { id: 'strategy', label: '策略管理', icon: Sliders },
    { id: 'backtest', label: '回测复盘', icon: History },
  ];

  const hasAge = realtimeAgeSeconds !== null && Number.isFinite(realtimeAgeSeconds);
  const isFresh = hasAge && realtimeStatus === 'ok' && (realtimeAgeSeconds as number) <= FRESH_SECONDS;
  const ageText = hasAge ? `${realtimeAgeSeconds}s` : '—';

  return (
    <aside className="w-56 bg-neutral-900/90 border-r border-neutral-800 flex flex-col justify-between shrink-0 select-none">
      <div className="flex flex-col">
        {/* Title Bar */}
        <div className="px-4 py-5 border-b border-neutral-800 flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-emerald-500/20 border border-emerald-500/40 flex items-center justify-center text-emerald-400 shrink-0">
            <Activity className="w-5 h-5" />
          </div>
          <div className="flex flex-col min-w-0">
            <h2 className="text-base font-bold text-neutral-100 tracking-wide truncate">趋势研判系统</h2>
            <span className="text-xs text-neutral-400 font-mono">v1.2.0 • Pro</span>
          </div>
        </div>

        {/* Section Label: 大类导航 */}
        <div className="px-4 pt-6 pb-3 text-xs font-bold text-neutral-400 uppercase tracking-wider flex items-center justify-between">
          <span>大类导航</span>
          <Layers className="w-4 h-4 text-neutral-500" />
        </div>

        {/* Navigation Items: 加大字体、图标并舒展纵向间距 */}
        <nav className="px-3 space-y-3">
          {navItems.map((item) => {
            const Icon = item.icon;
            const isActive = currentNav === item.id;
            return (
              <button
                key={item.id}
                onClick={() => onSelectNav(item.id)}
                className={`w-full flex items-center justify-between px-3.5 py-3.5 rounded-lg text-base font-semibold transition-all ${
                  isActive
                    ? 'bg-neutral-800 text-emerald-400 shadow-sm border border-neutral-700/60'
                    : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800/50'
                }`}
              >
                <div className="flex items-center gap-3">
                  <Icon className={`w-5 h-5 ${isActive ? 'text-emerald-400' : 'text-neutral-400'}`} />
                  <span>{item.label}</span>
                </div>
                {item.badge && (
                  <span className="text-xs px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 font-mono font-bold">
                    {item.badge}
                  </span>
                )}
              </button>
            );
          })}
        </nav>
      </div>

      {/* Live feed status */}
      <div className="p-4 border-t border-neutral-800">
        <div className="flex items-center justify-between px-1 text-xs text-neutral-400">
          <span className="flex items-center gap-2">
            <span
              className={`w-2 h-2 rounded-full ${
                isFresh ? 'bg-emerald-400' : 'bg-neutral-600'
              }`}
            ></span>
            <span>WebSocket</span>
          </span>
          <span className={`font-mono ${isFresh ? 'text-emerald-400 font-semibold' : 'text-neutral-500'}`}>
            {ageText}
          </span>
        </div>
      </div>
    </aside>
  );
};
