/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import {
  LayoutDashboard,
  Database,
  Sliders,
  History,
  Image as ImageIcon,
  Activity,
  Layers,
  Radio
} from 'lucide-react';

interface NavigationSidebarProps {
  currentNav: string;
  onSelectNav: (nav: string) => void;
  onOpenSketchDrawer: () => void;
}

export const NavigationSidebar: React.FC<NavigationSidebarProps> = ({
  currentNav,
  onSelectNav,
  onOpenSketchDrawer,
}) => {
  const navItems = [
    { id: 'workbench', label: '交易工作台', icon: LayoutDashboard, badge: 'Live' },
    { id: 'datacenter', label: '数据中心', icon: Database },
    { id: 'strategy', label: '策略管理', icon: Sliders },
    { id: 'backtest', label: '回测复盘', icon: History },
  ];

  return (
    <aside className="w-40 sm:w-44 bg-neutral-900/90 border-r border-neutral-800 flex flex-col justify-between shrink-0 select-none">
      <div className="flex flex-col">
        {/* Title Bar */}
        <div className="px-4 py-4 border-b border-neutral-800 flex items-center gap-2">
          <div className="w-7 h-7 rounded-lg bg-emerald-500/20 border border-emerald-500/40 flex items-center justify-center text-emerald-400">
            <Activity className="w-4 h-4" />
          </div>
          <div>
            <h2 className="text-xs font-bold text-neutral-100 tracking-wide">趋势研判系统</h2>
            <span className="text-[10px] text-neutral-500 font-mono">v1.2.0 • Pro</span>
          </div>
        </div>

        {/* Section Label: 大类导航 */}
        <div className="px-4 pt-4 pb-2 text-[11px] font-bold text-neutral-400 uppercase tracking-wider flex items-center justify-between">
          <span>大类导航</span>
          <Layers className="w-3 h-3 text-neutral-600" />
        </div>

        {/* Navigation Items */}
        <nav className="px-2 space-y-1">
          {navItems.map((item) => {
            const Icon = item.icon;
            const isActive = currentNav === item.id;
            return (
              <button
                key={item.id}
                onClick={() => onSelectNav(item.id)}
                className={`w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-xs font-medium transition-all ${
                  isActive
                    ? 'bg-neutral-800 text-emerald-400 font-semibold shadow-sm border border-neutral-700/60'
                    : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800/50'
                }`}
              >
                <div className="flex items-center gap-2.5">
                  <Icon className={`w-4 h-4 ${isActive ? 'text-emerald-400' : 'text-neutral-500'}`} />
                  <span>{item.label}</span>
                </div>
                {item.badge && (
                  <span className="text-[9px] px-1.5 py-0.2 rounded-full bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 font-mono animate-pulse">
                    {item.badge}
                  </span>
                )}
              </button>
            );
          })}
        </nav>
      </div>

      {/* Bottom Action: Original Sketches Drawer */}
      <div className="p-3 border-t border-neutral-800 space-y-2">
        <button
          onClick={onOpenSketchDrawer}
          className="w-full flex items-center justify-center gap-2 py-2 px-2.5 rounded-lg bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 text-xs font-medium border border-indigo-500/30 transition-colors shadow-sm"
        >
          <ImageIcon className="w-3.5 h-3.5" />
          <span>原草图图纸比对</span>
        </button>

        <div className="flex items-center justify-between px-1 text-[10px] text-neutral-500">
          <span className="flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-ping"></span>
            WebSocket
          </span>
          <span className="text-emerald-400 font-mono">18ms</span>
        </div>
      </div>
    </aside>
  );
};
