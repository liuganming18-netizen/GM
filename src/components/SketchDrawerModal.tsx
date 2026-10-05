/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { X, Download, ZoomIn, ZoomOut, Maximize2, ExternalLink, Image as ImageIcon } from 'lucide-react';

interface SketchDrawerModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export const SketchDrawerModal: React.FC<SketchDrawerModalProps> = ({ isOpen, onClose }) => {
  const [activeSketchIndex, setActiveSketchIndex] = useState(0);
  const [zoom, setZoom] = useState(1);

  if (!isOpen) return null;

  const sketches = [
    {
      title: '44-原草图格式渲染-1800x1180.png',
      type: 'PNG 高清位图',
      size: '360.9 KB',
      src: '/sketches/44-原草图格式渲染-1800x1180.png',
      desc: '当前渲染稿预览图（1800×1180）：全宽K线、成交量副图、OI曲线与双持仓监控。',
    },
    {
      title: '按原草图格式渲染-交易工作台.svg',
      type: 'SVG 矢量设计稿',
      size: '20.5 KB',
      src: '/sketches/按原草图格式渲染-交易工作台.svg',
      desc: '当前确定设计规范：代币仅简称、无“资格”元素、主表七组字段、展开区纵向排布。',
    },
    {
      title: '数据中心全页面草图.svg',
      type: 'SVG 原始设计依据',
      size: '22.3 KB',
      src: '/sketches/数据中心全页面草图.svg',
      desc: '数据中心完整页面布局草图（1800×1180）：K线周期覆盖主表、导入任务队列与缺段查看。',
    },
    {
      title: '策略管理全页面草图.svg',
      type: 'SVG 原始设计依据',
      size: '15.0 KB',
      src: '/sketches/策略管理全页面草图.svg',
      desc: '策略管理完整页面布局草图（1800×1180）：6阶段策略条件序列、阈值不杜撰、风控与手机提醒。',
    },
    {
      title: '回测复盘全页面草图.svg',
      type: 'SVG 原始设计依据',
      size: '17.6 KB',
      src: '/sketches/回测复盘全页面草图.svg',
      desc: '回测复盘完整页面布局草图（1800×1180）：净值/回撤双曲线槽位、留存记录清单与回放限制。',
    },
    {
      title: '交易工作台布局草图.svg',
      type: 'SVG 原始依据稿',
      size: '31.4 KB',
      src: '/sketches/交易工作台布局草图.svg',
      desc: '原始讨论定下的 1800×1180 草图原件，原样保留未改动。',
    },
  ];

  const current = sketches[activeSketchIndex];

  return (
    <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex justify-center items-center p-3 sm:p-6 animate-in fade-in">
      <div className="w-full max-w-6xl h-[90vh] bg-neutral-900 border border-neutral-700 rounded-2xl flex flex-col overflow-hidden shadow-2xl">
        {/* Modal Top Bar */}
        <div className="px-5 py-3.5 border-b border-neutral-800 flex items-center justify-between gap-4 bg-neutral-950/70">
          <div className="flex items-center gap-2">
            <ImageIcon className="w-5 h-5 text-emerald-400" />
            <h3 className="font-bold text-sm text-neutral-100">原设计草图对比与检视器 (1800×1180)</h3>
            <span className="text-[10px] px-2 py-0.5 rounded bg-neutral-800 text-neutral-300 font-mono">
              GitHub 仓库原件
            </span>
          </div>

          <div className="flex items-center gap-2">
            <a
              href={current.src}
              download
              target="_blank"
              rel="noopener noreferrer"
              className="px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold flex items-center gap-1.5 transition-colors"
            >
              <Download className="w-3.5 h-3.5" />
              下载当前图纸
            </a>
            <button
              onClick={onClose}
              className="p-1.5 rounded-lg hover:bg-neutral-800 text-neutral-400 hover:text-neutral-100 transition-colors"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {/* Tab Selector */}
        <div className="px-5 py-2.5 bg-neutral-950/40 border-b border-neutral-800 flex flex-wrap items-center justify-between gap-3 text-xs">
          <div className="flex gap-2">
            {sketches.map((sk, idx) => (
              <button
                key={idx}
                onClick={() => {
                  setActiveSketchIndex(idx);
                  setZoom(1);
                }}
                className={`px-3 py-1.5 rounded-lg font-medium transition-colors ${
                  activeSketchIndex === idx
                    ? 'bg-neutral-800 text-emerald-400 font-bold border border-neutral-700'
                    : 'text-neutral-400 hover:text-neutral-200'
                }`}
              >
                {sk.title}
              </button>
            ))}
          </div>

          <div className="flex items-center gap-2 text-neutral-400">
            <button
              onClick={() => setZoom((z) => Math.max(0.5, Number((z - 0.2).toFixed(1))))}
              className="p-1 rounded hover:bg-neutral-800 text-neutral-300"
            >
              <ZoomOut className="w-4 h-4" />
            </button>
            <span className="font-mono text-xs w-10 text-center">{Math.round(zoom * 100)}%</span>
            <button
              onClick={() => setZoom((z) => Math.min(2.5, Number((z + 0.2).toFixed(1))))}
              className="p-1 rounded hover:bg-neutral-800 text-neutral-300"
            >
              <ZoomIn className="w-4 h-4" />
            </button>
            <button
              onClick={() => setZoom(1)}
              className="px-2 py-0.5 rounded hover:bg-neutral-800 text-neutral-300 text-[11px]"
            >
              重置
            </button>
          </div>
        </div>

        {/* Canvas Body */}
        <div className="flex-1 overflow-auto bg-neutral-950 p-4 flex justify-center items-center">
          <div className="bg-white rounded-xl p-2 shadow-2xl max-w-none">
            <img
              src={current.src}
              alt={current.title}
              style={{ transform: `scale(${zoom})`, transformOrigin: 'top center' }}
              className="max-w-none transition-transform duration-100"
            />
          </div>
        </div>

        {/* Footer info */}
        <div className="px-5 py-2.5 bg-neutral-950 border-t border-neutral-800 text-xs text-neutral-400 flex items-center justify-between">
          <span>{current.desc}</span>
          <span className="font-mono text-neutral-500">规格: 1800 × 1180 px</span>
        </div>
      </div>
    </div>
  );
};
